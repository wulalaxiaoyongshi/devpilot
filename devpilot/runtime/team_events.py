"""V5.5：非阻塞多 Agent 任务调度 + 完成事件 + Lead 通知。"""  # V4 不改造为 async

import json  # 将 Cron 动作提交结果转换为字符串
from collections.abc import Callable  # 声明可选的外部回调
from threading import RLock  # 避免并发重复分配队友和任务
from typing import TYPE_CHECKING  # 避免在运行时硬依赖 V4 所有实现

from devpilot.background.manager import BackgroundJobManager  # V5.1 后台执行接口
from devpilot.background.model import BackgroundJob, JobContext  # Job 快照和取消上下文
from devpilot.events.bridge import make_job_event_callback, reconcile_terminal_jobs  # V5.2 回调桥接与补偿
from devpilot.events.bus import EventBus  # V5.2 线程安全事件队列
from devpilot.events.dispatcher import EventDispatcher  # V5.2 Lead 事件分发器
from devpilot.events.model import RuntimeEvent  # V5.2 标准事件结构
from devpilot.runtime.notifications import LeadNotice, LeadNoticeStore  # 持久化 Lead 通知

if TYPE_CHECKING:  # 类型检查时才引入 V4 对象
    from devpilot.agents.team import TeamManager  # V4 原来的 TeamManager 类型
    from devpilot.scheduler.manager import CronScheduler  # V5.4 定时调度器类型


class EventTeamRuntime:  # 负责把 V4 一次任务执行变成 V5 后台 Job
    def __init__(self, team: "TeamManager", jobs: BackgroundJobManager, bus: EventBus, dispatcher: EventDispatcher, notices: LeadNoticeStore):  # 注入之前各阶段的真实组件
        if dispatcher.bus is not bus:  # 确保分发器和桥接器使用同一个事件总线
            raise ValueError("Dispatcher and runtime must share the same EventBus")  # 防止事件送错队列
        self.team = team  # 保留 V4 TeamManager，不替换它的执行逻辑
        self.jobs = jobs  # V5.1 后台任务管理器
        self.bus = bus  # V5.2 事件总线
        self.dispatcher = dispatcher  # V5.2 事件分发器
        self.notices = notices  # Lead 持久通知日志
        self._lock = RLock()  # 保护任务/队友的临时预约状态
        self._reserved_tasks: set[str] = set()  # 正在排队或执行的任务 ID
        self._reserved_agents: set[str] = set()  # 正在排队或执行的队友 ID
        self._assignment_by_job: dict[str, tuple[str, str]] = {}  # Job ID -> (Task ID, Agent ID)
        self._approved_dependents: set[str] = set()  # 已外部检查并允许开始的下游任务
        self._preferred_agents: dict[str, str] = {}  # 下游任务可绑定已更新工作区的 Worker
        publisher = make_job_event_callback(bus)  # 将 V5.1 终态转换成 Event
        previous_callback = jobs.on_finished  # 可能已有其他订阅完成回调
        def forward_job(job: BackgroundJob) -> None:  # 一个 Job 可以通知多个观察者
            publisher(job)  # 优先发布可靠事件
            if previous_callback is not None:  # 保留此前注册的可选回调
                previous_callback(job)  # 调用先前的监听者
        jobs.on_finished = forward_job  # 不必修改 V5.1 的源码
        for event_type in ("job.completed", "job.failed", "job.cancelled", "job.interrupted"):  # 监听所有终态
            self.dispatcher.register(event_type, self._handle_job_event)  # 将事件转成通知并释放预约
        self._recover_interrupted_tasks()  # 把上次进程中断的 V4 Task 标记失败
        reconcile_terminal_jobs(self.jobs.store, self.bus)  # 补发之前未成功发布的终态事件

    def _recover_interrupted_tasks(self) -> None:  # 使重启后的 Job / Task 状态保持一致
        for job in self.jobs.store.all():  # 检查全部旧 Job 快照
            if job.status != "interrupted" or not job.name.startswith("team:") or job.task_id is None:  # 只处理本运行时产生的中断团队任务
                continue  # 其他类型的 Job 不归这里处理
            task = self.team.task_store.get(job.task_id)  # 查看对应的持久化 Task
            if task is None or task.status != "in_progress" or task.owner is None:  # 完成或不存在的任务不能被覆盖
                continue  # 已被其他恢复流程处理过
            self.team.ownership.fail(task.id, task.owner, f"Background job {job.id} interrupted")  # 保留失败状态等待 Lead 决策

    def approve_dependent(self, task_id: str, integrated: bool = False, worker_id: str | None = None) -> None:  # 人工确认代码已合并，并可指定拥有完整代码的 Worker
        if not integrated:  # 审核没有完成，禁止手动绕过安全门
            raise ValueError("Verify merges and worker worktree visibility before approval")  # 明确提示风险
        task = self.team.task_store.get(task_id)  # 读取持久化任务
        if task is None:  # ID 必须真实存在
            raise KeyError(f"Unknown task: {task_id}")  # 报错而不是静默执行
        if self.team.graph.is_blocked(task):  # 即使用户批准，依赖状态仍须 completed
            raise ValueError(f"Task dependencies are not completed: {task_id}")  # 阻止越过 Task Graph
        with self._lock:  # 修改共享批准集合
            if worker_id is not None and worker_id not in self.team.teammates:  # 指定的 Worker 必须已注册
                raise ValueError(f"Unknown worker: {worker_id}")  # 不允许虚构队友
            self._approved_dependents.add(task_id)  # 仅允许该下游任务启动
            if worker_id is not None:  # 可以绑定最新 Worktree 的 Worker
                self._preferred_agents[task_id] = worker_id  # 调度时尊重此目标 Worker

    def submit_ready(self, max_assignments: int | None = None) -> dict[str, str]:  # 非阻塞提交本轮可执行任务；返回 Task ID -> Job ID
        if max_assignments is not None and max_assignments < 1:  # 校验上限
            raise ValueError("max_assignments must be >= 1")  # 不接受 0 或负数
        started: dict[str, str] = {}  # 记录本轮实际提交成功的任务
        with self._lock:  # 从选择到后台提交全程持锁，避免重复分配
            runnable = self.team.graph.runnable()  # 获取 V3/V4 依赖判断结果
            available = [(id_, teammate) for id_, teammate in self.team.teammates.items() if not teammate.busy and id_ not in self._reserved_agents]  # 过滤已占用的 Worker
            limit = min(len(runnable), len(available))  # 每个 Worker 同时只能接一个任务
            if max_assignments is not None:  # 用户可能要求限制本次提交数量
                limit = min(limit, max_assignments)  # 尊重业务上限
            used_agents: set[str] = set()  # 当前批次已经分配的 Worker
            for task in runnable:  # 按 V3 TaskStore 持久顺序处理
                if len(started) >= limit:  # 本轮已无可用 Worker
                    break  # 结束任务调度
                if task.id in self._reserved_tasks:  # 任务可能已经在后台排队
                    continue  # 绝不能重复提交
                if task.depends_on and task.id not in self._approved_dependents:  # Worktree 代码可能尚未汇总
                    continue  # 默认阻止自动执行下游任务
                candidate_ids = [agent for agent, _ in available if agent not in used_agents]  # 本批次尚未选择的队友
                preferred = self._preferred_agents.get(task.id)  # 下游任务可能要求特定新 Worktree
                if preferred is not None and preferred not in candidate_ids:  # 指定队友不空闲时不能悄悄换人
                    continue  # 等该队友准备好
                if not candidate_ids:  # 当前没有任何可用队友
                    break  # 结束分配
                agent_id = preferred or candidate_ids[0]  # 优先使用集成审查指定的 Worker
                self._reserved_tasks.add(task.id)  # 先预约任务，防止另一个提交线程抢到
                self._reserved_agents.add(agent_id)  # 预约队友，防止重复分配
                def run_worker(context: JobContext, chosen_task: str = task.id, chosen_agent: str = agent_id) -> str:  # 与 V5.1 约定的任务函数签名一致
                    context.raise_if_cancelled()  # 若尚未开始就取消，则直接退出
                    return self.team._run_assignment(chosen_task, chosen_agent)  # 复用 V4 的 claim、Worker、commit 和 complete/fail
                try:  # submit() 理论上可能因关闭或磁盘错误失败
                    job_id = self.jobs.submit(name=f"team:{task.id}:{agent_id}", function=run_worker, task_id=task.id)  # 创建可持久化后台 Job
                except Exception:  # 提交失败时必须撤销预约
                    self._reserved_tasks.discard(task.id)  # 释放任务
                    self._reserved_agents.discard(agent_id)  # 释放队友
                    raise  # 原样向 Lead 报告失败
                self._assignment_by_job[job_id] = (task.id, agent_id)  # 记录 Job 与队友的对应关系
                started[task.id] = job_id  # 记录非阻塞提交成功的 Job ID
                used_agents.add(agent_id)  # 防止同批次重复指派队友
                self._approved_dependents.discard(task.id)  # 一次批准只对当前提交生效
                self._preferred_agents.pop(task.id, None)  # 提交成功后消费一次性 Worker 指派
        return started  # 不等待 Worker 执行结束

    def _handle_job_event(self, event: RuntimeEvent) -> None:  # 由 Lead 所在线程调用，而不是 Worker 修改 Lead Context
        job = self.jobs.get_status(event.job_id)  # 查询 V5.1 保存的执行终态
        if job.status not in {"completed", "failed", "cancelled", "interrupted"}:  # 确保事件确实指向终态 Job
            raise RuntimeError(f"Event {event.id} refers to non-terminal job {job.id}")  # 不允许提前 ACK
        summary = f"{job.name}: {job.status}"  # 构建 Lead 可读摘要
        if job.status == "completed":  # 成功任务有结果
            summary += f"; result={job.result or ''}"  # 展示 Worker 输出
        else:  # 失败/取消/中断的任务有原因
            summary += f"; error={job.error or ''}"  # 展示异常或取消信息
        self.notices.add_once(LeadNotice(event.id, job.id, event.type, job.task_id, summary))  # 先持久化通知再允许 Event ACK
        with self._lock:  # 保证预约列表的一致性
            assignment = self._assignment_by_job.pop(job.id, None)  # 找出本次 Job 的预约关系
            if assignment is not None:  # 当前进程提交的团队任务
                task_id, agent_id = assignment  # 分别取出 Task 和队友 ID
                self._reserved_tasks.discard(task_id)  # 释放完成/失败/取消的任务预约
                self._reserved_agents.discard(agent_id)  # 队友可以接新的任务

    def process_events(self, max_events: int = 100) -> int:  # 让 Lead 非阻塞处理已到达的事件
        return self.dispatcher.process_available(max_events)  # 返回已确认的事件数量

    def pending_assignments(self) -> dict[str, str]:  # 非阻塞查询仍在预约的 Task -> Job
        with self._lock:  # 防止和提交操作并发修改
            return {task_id: job_id for job_id, (task_id, _) in self._assignment_by_job.items()}  # 返回独立快照

    def register_cron_action(self, scheduler: "CronScheduler", action_name: str = "team.run_ready") -> None:  # 与 V5.4 CronScheduler 连接
        def run_team_tick(context: JobContext, payload: dict) -> str:  # 适配 CronAction(context, payload) 签名
            context.raise_if_cancelled()  # 禁止已经取消的触发再次提交工作
            limit = payload.get("max_assignments")  # 读取持久化参数中可选的并发上限
            if limit is not None and (type(limit) is not int or limit < 1):  # 严格校验 JSON 参数
                raise ValueError("max_assignments must be a positive integer")  # 报告无效 Cron 参数
            return json.dumps(self.submit_ready(max_assignments=limit), ensure_ascii=False)  # Cron Job 只负责提交 Worker 后台 Job
        scheduler.register_action(action_name, run_team_tick)  # 将稳定动作名称绑定到本次进程的函数

    def shutdown(self, cron: "CronScheduler | None" = None) -> None:  # 优雅关闭本进程的定时器和线程池
        if cron is not None:  # 如果启动过 Cron
            cron.stop()  # 先停止新的计划提交
        self.jobs.shutdown(wait_for_jobs=True)  # 等待运行中任务结束并让终态事件发布
        while self.process_events() > 0:  # 把已到达的完成事件持久化为 Lead 通知
            pass  # 队列为空才结束
