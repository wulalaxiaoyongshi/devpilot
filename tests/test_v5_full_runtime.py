"""V5.6 完整集成测试：要求安装用户项目中真实 V4 模块，V5.1～V5.5 不使用替身。"""  # 不调用真实大模型

import json  # 解析 Cron 后台 Job 的输出
import subprocess  # 创建真实 Git 临时仓库
from datetime import datetime, timedelta, timezone  # 用模拟时间验证 Cron
from pathlib import Path  # 表示项目和状态目录
from threading import Barrier, Event  # 控制并发而非 sleep 猜测时序

import pytest  # 使用临时目录和异常断言

from devpilot.agents.mailbox import MailboxStore  # 引入真实 V4 邮箱
from devpilot.agents.team import TeamManager  # 引入真实 V4 团队调度
from devpilot.background.manager import BackgroundJobManager  # 引入 V5.1 Job 运行时
from devpilot.background.model import BackgroundJob  # 构造中断状态测试记录
from devpilot.background.process import BackgroundProcessRunner, ProcessSpec  # 引入 V5.3 进程执行器
from devpilot.background.store import JobStore  # 引入 V5.1 持久化状态
from devpilot.events.bus import EventBus  # 引入 V5.2 总线
from devpilot.events.dispatcher import EventDispatcher  # 引入 V5.2 分发器
from devpilot.events.journal import EventJournal  # 引入 V5.2 事件日志
from devpilot.runtime.notifications import LeadNoticeStore  # 引入 V5.5 Lead 通知
from devpilot.runtime.team_events import EventTeamRuntime  # 引入 V5.5 统一调度
from devpilot.scheduler.manager import CronScheduler  # 引入 V5.4 定时器
from devpilot.scheduler.store import CronStore  # 引入 V5.4 持久化计划
from devpilot.tasks.store import TaskStore  # 引入真实 V4 持久 Task
from devpilot.workspace.worktree import WorktreeManager  # 引入真实 V4 Worktree


def git(cwd: Path, *args: str) -> str:  # 运行 Git 命令并返回标准输出
    finished = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)  # 使用 argv，避免 Shell
    return finished.stdout.strip()  # 返回标准输出文本


def make_repo(tmp_path: Path) -> Path:  # 创建不会修改用户 Git 仓库的临时项目
    repo = tmp_path / "repo"  # Git 主工作区位于临时目录
    repo.mkdir()  # 创建空目录
    git(repo, "init")  # 初始化版本库
    git(repo, "config", "user.name", "DevPilot V5.6 Test")  # 仅配置本仓库的 Git 用户名
    git(repo, "config", "user.email", "devpilot-v56@example.com")  # 仅配置本仓库邮箱
    (repo / "README.md").write_text("# Isolated test repo\n", encoding="utf-8")  # 添加初始可提交文件
    git(repo, "add", "README.md")  # 暂存初始文件
    git(repo, "commit", "-m", "initial commit")  # 建立 Worktree 所需 HEAD
    return repo  # 返回主仓库路径


class FakeCodingAgent:  # 真实 V4 Teammate 会调用这个确定性 Agent
    def __init__(self, workdir: Path, agent_id: str, rendezvous: Barrier | None = None, enter: Event | None = None, release: Event | None = None, failure: bool = False):  # 注入并发同步点和失败开关
        self.workdir = workdir  # 每个 Worker 拥有独立的工作目录
        self.agent_id = agent_id  # 保存 Worker 身份
        self.messages: list[str] = []  # 模拟 Persistent Teammate 的独立消息历史
        self.rendezvous = rendezvous  # 两个 Worker 并行时使用的 Barrier
        self.enter = enter  # 可以通知主线程任务已真正开始
        self.release = release  # 可以暂停 Worker 以观察非阻塞状态
        self.failure = failure  # 控制是否故意抛出异常

    def run(self, prompt: str) -> str:  # 对齐真实 V4 Agent.run 接口
        self.messages.append(prompt)  # 保存这次任务的上下文记录
        if self.enter is not None:  # 若测试需要检查任务是否开始
            self.enter.set()  # 通知主线程 Worker 已进入 run()
        if self.rendezvous is not None:  # 若属于第一批两个并行 Worker
            self.rendezvous.wait(timeout=10)  # 必须两人都启动才能继续；串行实现将超时失败
        if self.release is not None and not self.release.wait(timeout=10):  # 可选择人为阻塞 Worker
            raise TimeoutError("Fake worker was not released")  # 避免测试永久挂起
        if self.failure:  # 测试失败分支时注入异常
            raise RuntimeError("intentional worker failure")  # 由 V4/JobManager 分别写入失败状态
        if self.agent_id == "integrator":  # 集成 Worker 必须来自合并后的主分支
            assert (self.workdir / "backend.txt").is_file()  # 验证 Backend 修改确实可见
            assert (self.workdir / "tests.txt").is_file()  # 验证 Tests 修改确实可见
        output = self.workdir / f"{self.agent_id}.txt"  # Worker 只能修改自己的 Git Worktree
        old_text = output.read_text(encoding="utf-8") if output.exists() else ""  # 读取当前 Worker 之前的成果
        output.write_text(old_text + f"{self.agent_id}:call={len(self.messages)}\n", encoding="utf-8")  # 在 Worktree 写入可 Commit 文件
        return f"{self.agent_id} completed call {len(self.messages)}"  # 返回确定性的任务结果


def make_runtime(tmp_path: Path, repo: Path, tasks: TaskStore, workers: tuple[str, ...], factory, max_workers: int = 4):  # 组装真正的 V4 和 V5 组件
    state = tmp_path / "state"  # Runtime 状态放在主 Git 仓库之外，避免污染 Merge
    mailbox = MailboxStore(state / "mailbox.json")  # V4 持久邮箱
    worktrees = WorktreeManager(repo, tmp_path / "worktrees")  # V4 工作目录隔离
    team = TeamManager(tasks, mailbox, worktrees, factory)  # 真实 V4 TeamManager
    for worker_id in workers:  # 注册本测试需要的 Worker
        team.add_teammate(worker_id, worker_id)  # 为每个 Worker 创建独立 Agent/Worktree
    jobs = BackgroundJobManager(JobStore(state / "jobs.json"), max_workers=max_workers)  # V5.1 运行时和 JobStore
    bus = EventBus(EventJournal(state / "events.jsonl"))  # V5.2 内存队列 + 持久事件日志
    dispatcher = EventDispatcher(bus)  # V5.2 Dispatcher
    notices = LeadNoticeStore(state / "lead_notices.json")  # V5.5 Lead 通知 Store
    runtime = EventTeamRuntime(team, jobs, bus, dispatcher, notices)  # 连接所有真实 V4/V5 组件
    return runtime, team, jobs, bus, notices, mailbox, worktrees  # 返回测试所需观察对象


def test_v5_full_cron_parallel_merge_and_lead_notice(tmp_path: Path):  # V5 主闭环：Cron -> Worker -> Git -> Event -> Lead -> 集成
    repo = make_repo(tmp_path)  # 创建真实但隔离的 Git 仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 创建真实 V4 TaskStore
    task_a = tasks.create("backend")  # 创建第一个无依赖任务
    task_b = tasks.create("tests")  # 创建第二个无依赖任务
    task_c = tasks.create("integration", depends_on=[task_a.id, task_b.id])  # C 只能在 A/B 完成后开始
    barrier = Barrier(3)  # 两个 Worker 和主测试线程共同证明发生并发
    agents: dict[str, FakeCodingAgent] = {}  # 记录 Worker，以检查 Context 是否隔离

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # 对齐 V4 TeamManager 的 AgentFactory
        concurrent_barrier = barrier if agent_id in {"backend", "tests"} else None  # 只让第一批 Worker 使用同步点
        agent = FakeCodingAgent(workdir, agent_id, rendezvous=concurrent_barrier)  # 创建独立的 Fake Coding Agent
        agents[agent_id] = agent  # 保存实例，便于测试内部状态
        return agent  # 返回给真实 V4 Teammate

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("backend", "tests"), factory)  # 构造统一 Runtime
    clock_time = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)  # 固定模拟时钟，不依赖当前真实日期
    cron = CronScheduler(CronStore(tmp_path / "state" / "cron.json"), jobs, clock=lambda: clock_time)  # 注入 V5.4 持久化 Cron
    runtime.register_cron_action(cron)  # 把动作 team.run_ready 注册到 Cron
    try:  # 测试失败也保证线程池关闭
        schedule = cron.create("team tick", "*/5 * * * *", "team.run_ready", now=clock_time)  # 每五分钟检查一次团队可执行任务
        tick = cron.run_due(now=clock_time + timedelta(minutes=5))  # 用模拟时钟让任务触发
        cron_job_id = tick.started[schedule.id]  # 获取 Cron 任务自己的后台 Job ID
        assert jobs.wait(cron_job_id, timeout=8).status == "completed"  # Cron 提交动作本身必须成功
        task_jobs = json.loads(jobs.get_result(cron_job_id))  # Cron Job 返回两个 Worker Job ID
        assert set(task_jobs) == {task_a.id, task_b.id}  # 第一次只允许 A/B 开始
        assert cron.run_due(now=clock_time + timedelta(minutes=5)).started == {}  # 同一个 Cron 槽位不得二次触发
        assert not (repo / "backend.txt").exists()  # Worker 尚未 Merge，不得污染主仓库
        assert not (repo / "tests.txt").exists()  # 第二个 Worker 同样隔离
        barrier.wait(timeout=10)  # 如果 Worker 实际串行，则等不到两者并导致失败
        assert all(jobs.wait(job_id, timeout=10).status == "completed" for job_id in task_jobs.values())  # 等待 A/B 完成
        assert runtime.process_events() == 3  # 消费 Cron 成功 + Worker A/B 成功事件
        assert runtime.pending_assignments() == {}  # 两个 Worker 完成后释放预约
        assert runtime.submit_ready() == {}  # C 虽然依赖完成，但尚未验证合并，不能自动执行
        assert tasks.get(task_c.id).status == "pending"  # C 必须保持 pending
        worktrees.merge_branch(team.teammates["backend"].worktree.branch)  # 将 Backend 分支真正合并回主分支
        worktrees.merge_branch(team.teammates["tests"].worktree.branch)  # 将 Tests 分支合并回主分支
        assert (repo / "backend.txt").is_file() and (repo / "tests.txt").is_file()  # 主分支现在已包含两份代码
        team.add_teammate("integrator", "Integration")  # 合并完成后再创建 Worktree，继承最新 HEAD
        runtime.approve_dependent(task_c.id, integrated=True, worker_id="integrator")  # 明确审核后放行下游任务
        final_job_id = runtime.submit_ready()[task_c.id]  # 下游 Job 现在才允许提交
        assert jobs.wait(final_job_id, timeout=10).status == "completed"  # Integrator 运行时将验证上游文件可见
        assert runtime.process_events() == 1  # 处理 C 的完成事件
        assert tasks.get(task_c.id).status == "completed"  # V4 Task 完整闭环
        assert tasks.get(task_c.id).owner == "integrator"  # C 被指定为集成 Worker
        assert len(mailbox.read("lead")) == 3  # V4 邮箱应有 A/B/C 三份结果
        assert len(notices.all()) == 4  # Cron Job、A、B、C 共四条 Lead 通知
        assert all(item.event_type == "job.completed" for item in notices.all())  # 全部事件均为成功终态
        assert len(agents["backend"].messages) == 1  # Backend Context 与其他队友独立
        assert len(agents["tests"].messages) == 1  # Tests Context 独立
        assert len(agents["integrator"].messages) == 1  # Integrator 是新创建的独立 Context
        assert EventBus(EventJournal(tmp_path / "state" / "events.jsonl")).pending_in_memory() == 0  # ACK 的事件不再重放
        assert all(item.status == "completed" for item in TaskStore(tmp_path / "state" / "tasks.json").all())  # 重开 Store 后 Task 持久化仍正确
    finally:  # 防止失败时 Barrier 卡住后台线程
        barrier.abort()  # 释放尚在 Barrier 等待的 Worker
        runtime.shutdown(cron)  # 按 Cron -> Jobs -> Events 顺序关闭


def test_v5_real_worker_failure_and_notification(tmp_path: Path):  # 验证 Worker 异常能穿透 V4、V5 Job 和事件通知
    repo = make_repo(tmp_path)  # 创建独立测试仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 创建真实任务持久化层
    task = tasks.create("deliberately failing")  # 创建单个失败测试 Task

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # Worker 构造工厂
        return FakeCodingAgent(workdir, agent_id, failure=True)  # 每次运行都主动报错

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("failing",), factory)  # 创建统一运行时
    try:  # 发生异常也清理资源
        job_id = runtime.submit_ready()[task.id]  # 异步启动这个 Task
        assert jobs.wait(job_id, timeout=10).status == "failed"  # JobManager 捕获异常并保存 failed
        assert tasks.get(task.id).status == "failed"  # V4 Ownership 同样记录失败
        assert runtime.process_events() == 1  # Lead 消费失败事件
        assert notices.all()[0].event_type == "job.failed"  # 通知类型正确
        assert "intentional worker failure" in notices.all()[0].summary  # 异常原因没有丢失
        assert (repo / "README.md").is_file()  # 主仓库仍保持原有文件
    finally:  # 回收线程
        runtime.shutdown()  # 刷新事件并停止 JobManager


def test_v5_unacked_event_replayed_after_restart(tmp_path: Path):  # 验证事件可在消费前重启后再次交付
    repo = make_repo(tmp_path)  # 创建临时仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 创建真实 TaskStore
    task = tasks.create("event persistence")  # 创建可以成功执行的任务

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # AgentFactory
        return FakeCodingAgent(workdir, agent_id)  # 生成确定性 Worker

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("a",), factory)  # 创建第一轮运行时
    job_id = runtime.submit_ready()[task.id]  # 先提交 Worker
    assert jobs.wait(job_id, timeout=10).status == "completed"  # 等待任务完成并发布事件
    jobs.shutdown(wait_for_jobs=True)  # 模拟旧进程退出，但故意不让 EventDispatcher 消费事件
    assert EventBus(EventJournal(tmp_path / "state" / "events.jsonl")).pending_in_memory() == 1  # 新 EventBus 可以找回未确认事件
    new_jobs = BackgroundJobManager(JobStore(tmp_path / "state" / "jobs.json"), max_workers=2)  # 重建 V5.1 运行时
    new_bus = EventBus(EventJournal(tmp_path / "state" / "events.jsonl"))  # 从日志恢复事件
    new_notices = LeadNoticeStore(tmp_path / "state" / "lead_notices.json")  # 重新打开通知持久化
    new_runtime = EventTeamRuntime(team, new_jobs, new_bus, EventDispatcher(new_bus), new_notices)  # 模拟 Runtime 重启
    try:  # 确保新线程池也退出
        assert new_runtime.process_events() == 1  # 新 Lead 消费恢复事件
        assert new_notices.all()[0].job_id == job_id  # 通知对应旧执行
        assert new_runtime.process_events() == 0  # 已经 ACK，不再重复消费
    finally:  # 释放新运行时
        new_runtime.shutdown()  # 安全停止


def test_v5_process_result_also_flows_through_event_bus(tmp_path: Path):  # 验证 V5.3 操作系统进程也可以进入事件链
    import sys  # 使用当前解释器，兼容 Windows 与 Linux
    workspace = tmp_path / "work"  # 创建独立工作区
    workspace.mkdir()  # 准备实际运行目录
    state = tmp_path / "state"  # 状态与 Git 无关
    jobs = BackgroundJobManager(JobStore(state / "jobs.json"), max_workers=2)  # V5.1 后台线程池
    bus = EventBus(EventJournal(state / "events.jsonl"))  # V5.2 EventBus
    from devpilot.events.bridge import make_job_event_callback  # V5.2 回调桥
    jobs.on_finished = make_job_event_callback(bus)  # 真实进程结束后由 JobManager 发事件
    received: list[str] = []  # 保存 Lead 收到的进程 Job ID
    dispatcher = EventDispatcher(bus)  # 为 Lead 创建 Dispatcher
    dispatcher.register("job.completed", lambda event: received.append(event.job_id))  # 注册完成处理器
    runner = BackgroundProcessRunner(workspace)  # V5.3 进程执行器
    spec = ProcessSpec(argv=(sys.executable, "-c", "print('v5-process-ok')"), timeout_seconds=15.0)  # 不依赖 Shell 的真实系统子进程
    try:  # 资源回收
        job_id = runner.submit(jobs, "process-test", spec)  # 立即启动独立系统进程
        assert jobs.wait(job_id, timeout=20).status == "completed"  # 等待真实进程正常退出
        assert "Exit code: 0" in jobs.get_result(job_id)  # 退出码为零
        assert "v5-process-ok" in jobs.get_result(job_id)  # stdout 被保存并可回读
        assert dispatcher.process_available() == 1  # Lead 消费操作系统进程完成事件
        assert received == [job_id]  # 回调取得正确 Job ID
    finally:  # 清理线程池
        jobs.shutdown()  # 完成后关闭


def test_v5_interrupted_job_reconciles_real_v4_task(tmp_path: Path):  # 验证重启后真实 V4 Task 和 V5 Job 同步为中断/失败
    repo = make_repo(tmp_path)  # 创建独立 Git 仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 创建真实任务存储
    task = tasks.create("previously interrupted")  # 创建模拟崩溃任务

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # 创建 Worker 实例
        return FakeCodingAgent(workdir, agent_id)  # 本测试不真正执行 Agent

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("a",), factory)  # 创建初始运行时
    runtime.shutdown()  # 关闭初始线程池以模拟下一次启动
    team.ownership.claim(task.id, "a")  # 将真实 V4 Task 设置成 in_progress，并持久化 owner
    old_store = JobStore(tmp_path / "state" / "jobs.json")  # 打开旧 JobStore
    old_store.add(BackgroundJob(id="job_interrupted_demo", name=f"team:{task.id}:a", task_id=task.id, status="running"))  # 模拟崩溃时未完成的 Job
    new_jobs = BackgroundJobManager(JobStore(tmp_path / "state" / "jobs.json"), max_workers=2)  # 初始化时把 running Job 标记为 interrupted
    new_bus = EventBus(EventJournal(tmp_path / "state" / "events.jsonl"))  # 从旧 EventJournal 恢复事件
    new_notices = LeadNoticeStore(tmp_path / "state" / "lead_notices.json")  # 重新打开通知文件
    new_runtime = EventTeamRuntime(team, new_jobs, new_bus, EventDispatcher(new_bus), new_notices)  # 初始化期间同步 V4 Task 失败状态
    try:  # 测试后关闭
        assert new_jobs.get_status("job_interrupted_demo").status == "interrupted"  # 真实 Job 已恢复成中断
        assert tasks.get(task.id).status == "failed"  # 真实 V4 Task 不再永久卡在 in_progress
        assert new_runtime.process_events() == 1  # 中断终态产生可恢复事件
        assert new_notices.all()[0].event_type == "job.interrupted"  # Lead 得到中断通知
    finally:  # 回收资源
        new_runtime.shutdown()  # 正常关闭


def test_v5_queued_cancellation_and_resubmission(tmp_path: Path):  # 验证真实 V4 Team 对排队取消和再次提交的处理
    repo = make_repo(tmp_path)  # 创建独立 Git 测试仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 创建真实 TaskStore
    first = tasks.create("long-running")  # 第一个任务占用唯一的后台线程
    second = tasks.create("cancel-queued")  # 第二个任务将处于后台排队状态
    entered = Event()  # 用于确认第一个任务已经启动
    release = Event()  # 用于控制第一任务何时完成

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # 创建可控的 Worker
        return FakeCodingAgent(workdir, agent_id, enter=entered, release=release) if agent_id == "first" else FakeCodingAgent(workdir, agent_id)  # 第一个 Worker 被阻塞，第二个不阻塞

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("first", "second"), factory, max_workers=1)  # 只有一个后台线程
    try:  # 保证运行结束后释放所有阻塞
        submitted = runtime.submit_ready()  # 两个 Task 都获得 Job ID，第二个排队
        assert set(submitted) == {first.id, second.id}  # 两个 Task 都成功提交
        assert entered.wait(timeout=5)  # 第一个任务实际占有执行线程
        assert jobs.cancel(submitted[second.id]) is True  # 排队 Job 可直接取消
        assert jobs.wait(submitted[second.id], timeout=5).status == "cancelled"  # 取消的 Job 不执行 Worker
        assert tasks.get(second.id).status == "pending"  # 尚未 claim 的 V4 Task 仍为 pending
        release.set()  # 让第一个 Worker 正常完成
        assert jobs.wait(submitted[first.id], timeout=10).status == "completed"  # 第一个 Job 完成
        assert runtime.process_events() == 2  # Lead 消费成功与取消事件
        assert runtime.pending_assignments() == {}  # 事件处理后释放所有预约
        retry = runtime.submit_ready()  # 再次寻找可执行的任务
        assert set(retry) == {second.id}  # 只有取消过的第二个 Task 需要补执行
        assert retry[second.id] != submitted[second.id]  # 新执行尝试必须产生新的 Job ID
        assert jobs.wait(retry[second.id], timeout=10).status == "completed"  # 重试真正执行成功
        assert runtime.process_events() == 1  # 消费最后一个完成事件
        assert tasks.get(second.id).status == "completed"  # V4 Task 进入 completed
        assert {notice.event_type for notice in notices.all()} == {"job.completed", "job.cancelled"}  # Lead 看到取消与成功事件
    finally:  # 不允许测试失败造成悬挂 Worker
        release.set()  # 放行等待中的 Worker
        runtime.shutdown()  # 停止 JobManager 并分发剩余事件


def test_v5_persistent_teammate_keeps_own_context(tmp_path: Path):  # 复用同一个 V4 Agent 对象，确认 Context 真正长期保留
    repo = make_repo(tmp_path)  # 创建新 Git 仓库
    tasks = TaskStore(tmp_path / "state" / "tasks.json")  # 真实 TaskStore
    task_a = tasks.create("first pass")  # 第一项短任务
    agents: dict[str, FakeCodingAgent] = {}  # 保存实例，以便观察会话历史

    def factory(workdir: Path, agent_id: str, mailbox: MailboxStore) -> FakeCodingAgent:  # 队友工厂
        worker = FakeCodingAgent(workdir, agent_id)  # 每名队友只创建一次 Agent
        agents[agent_id] = worker  # 记录实例，后续验证 Identity
        return worker  # 返回新 Agent

    runtime, team, jobs, bus, notices, mailbox, worktrees = make_runtime(tmp_path, repo, tasks, ("worker",), factory)  # 创建一个长期队友
    original_agent = team.teammates["worker"].agent  # 保存第一次创建的 Agent 引用
    try:  # 自动清理线程池
        first_job = runtime.submit_ready()[task_a.id]  # 启动第一次任务
        assert jobs.wait(first_job, timeout=10).status == "completed"  # 第一轮完成
        assert runtime.process_events() == 1  # 处理事件并释放 Worker 预约
        second_task = tasks.create("second pass")  # 第一轮结束后再创建新任务
        second_job = runtime.submit_ready()[second_task.id]  # 同一个 Worker 领取第二轮任务
        assert jobs.wait(second_job, timeout=10).status == "completed"  # 第二轮完成
        assert runtime.process_events() == 1  # 处理第二条事件
        assert team.teammates["worker"].agent is original_agent  # Agent 对象没有被重建
        assert len(agents["worker"].messages) == 2  # 同一个消息列表保留两轮任务历史
        assert (team.teammates["worker"].worktree.path / "worker.txt").read_text(encoding="utf-8").splitlines() == ["worker:call=1", "worker:call=2"]  # 同一个 Worktree 累积两次提交结果
        assert len(notices.all()) == 2  # Lead 收到两轮独立 Job 的事件通知
    finally:  # 关闭后台执行器
        runtime.shutdown()  # 确保线程退出
