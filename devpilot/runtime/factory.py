"""V5.7：在一处组装 V0～V5 的真实运行时，不再为每个 Demo 手动搭一套。"""  # 本文件只依赖过去阶段的公开接口

from collections.abc import Callable  # 用于注入模型工厂和审批函数
from pathlib import Path  # 项目根目录和状态目录
from threading import RLock  # 保护共享 Memory 和 Lead 会话
from typing import Any  # 支持不同具体模型实现

from devpilot.agents.mailbox import MailboxStore, create_mailbox_tools  # V4 共享邮箱及 Tool
from devpilot.agents.team import TeamManager  # V4 任务分配和 Worktree 执行
from devpilot.background.manager import BackgroundJobManager  # V5.1 长期后台线程池
from devpilot.background.store import JobStore  # V5.1 持久任务
from devpilot.context.manager import ContextManager  # V2 Context 和动态 Prompt
from devpilot.context.skills import SkillCatalog, create_load_skill_tool  # V2 按需 Skill
from devpilot.context.todo import TodoManager, create_todo_tool  # V2 当前 Todo
from devpilot.core.agent import Agent  # V0/V1/V2/V3 已实现的真实 Agent Loop
from devpilot.events.bus import EventBus  # V5.2 内存事件队列
from devpilot.events.dispatcher import EventDispatcher  # V5.2 事件分发
from devpilot.events.journal import EventJournal  # V5.2 可靠追加事件日志
from devpilot.memory.manager import MemoryManager  # V3 Memory 工具与提取器
from devpilot.memory.store import MemoryStore  # V3 共享知识数据库
from devpilot.runtime.lead_delivery import LeadCursorStore, LeadDelivery  # V5.7 事件→Lead 适配
from devpilot.runtime.notifications import LeadNoticeStore  # V5.5 持久通知
from devpilot.runtime.permission import IntegratedPermission  # V5.7 新工具权限
from devpilot.runtime.team_events import EventTeamRuntime  # V5.5 团队后台调度
from devpilot.runtime.tools import create_event_tools  # V5.7 V1 Tool 适配
from devpilot.scheduler.manager import CronScheduler  # V5.4 Cron 调度
from devpilot.scheduler.store import CronStore  # V5.4 定时计划持久化
from devpilot.tasks.scheduler import TaskScheduler  # V3 持久任务工具
from devpilot.tasks.store import TaskStore  # V3/V4 共享任务状态
from devpilot.tools.builtin.factory import create_builtin_registry  # V1 文件和 Bash Tools
from devpilot.tools.hooks import HookManager  # V1 PreToolUse Hook
from devpilot.tools.permissions import PermissionPolicy  # V1 原来的权限规则
from devpilot.workspace.worktree import WorktreeManager  # V4 Git 工作区隔离


class LockedMemoryManager(MemoryManager):  # 解决单进程多 Worker 访问 V3 JSON Memory 时的写入竞争
    def __init__(self, store: MemoryStore, model: Any, lock: RLock, auto_extract: bool = False):  # 注入共享 Lock
        super().__init__(store=store, model=model, auto_extract=auto_extract)  # 复用 V3 逻辑
        self._shared_memory_lock = lock  # 所有 Agent 使用同一把锁

    def remember(self, content: str, category: str, tags: list[str] | None = None, source: str = "agent") -> str:  # 显式保存长期知识
        with self._shared_memory_lock:  # 将 V3 的读取、去重和写入作为一个操作保护
            return super().remember(content, category, tags, source)  # 复用 V3 Consolidator

    def recall(self, query: str, limit: int = 5) -> str:  # 从共享 Memory 检索项目知识
        with self._shared_memory_lock:  # 避免和写入操作交错
            return super().recall(query, limit)  # 复用 V3 Recall

    def capture_turn(self, user_input: str, assistant_output: str) -> None:  # 在完整交互后可选自动提取知识
        if not self.auto_extract:  # 关闭时不额外调用模型
            return  # 当前不提取
        extracted = self.extractor.extract(user_input=user_input, assistant_output=assistant_output)  # 在锁外完成耗时模型提取
        with self._shared_memory_lock:  # 只锁住可能发生文件写入的部分
            for item in extracted:  # 保存提取到的每条知识
                self.consolidator.save(content=item.content, category=item.category, tags=item.tags, source="auto_extract")  # V3 原有精确去重


def create_app(repo_root: Path, model_factory: Callable[[str], Any], approve: Callable[[str, dict], bool] | None = None, state_dir: Path | None = None, max_workers: int = 4, auto_extract: bool = False):  # 统一构造 Lead / Worker / Runtime
    from devpilot.runtime.app import DevPilotApp  # 延迟导入，避免互相引用
    root = repo_root.resolve()  # 指定当前 Git 项目根目录
    if not root.is_dir():  # 不允许无效项目路径
        raise ValueError(f"Project directory does not exist: {root}")  # 尽早报错
    state = (state_dir or (root / ".devpilot")).resolve()  # 默认复用旧阶段的状态目录
    state.mkdir(parents=True, exist_ok=True)  # 确保数据目录存在
    task_store = TaskStore(state / "tasks.json")  # V3/V4 所有 Agent 共享的同一个任务 Store
    memory_store = MemoryStore(state / "memory.json")  # V3 所有 Agent 共享的一个 MemoryStore
    memory_lock = RLock()  # 保护单进程内的跨 Agent Memory 写入
    mailbox = MailboxStore(state / "mailbox.json")  # V4 共享邮箱
    worktrees = WorktreeManager(root, root.parent / (root.name + "-worktrees"))  # V4 独立 Git 工作区
    skills_root = root / "skills"  # Lead / Worker 共用 Skill 目录，但各自独立加载
    lead_lock = RLock()  # 防止 Event pump 和用户同时调用 Lead.run

    def build_agent(workdir: Path, agent_id: str, shared_mailbox: MailboxStore, is_lead: bool, team: Any = None, event_runtime: Any = None, delivery: Any = None, cron: Any = None) -> Agent:  # 统一构造真实 V0～V3 Agent
        model = model_factory(agent_id)  # 给每个 Agent 独立模型实例
        tools = create_builtin_registry(workdir)  # V1 Tool Registry + 绑定工作区的 Tool
        todos = TodoManager()  # 每个 Agent 独立 Todo
        tools.register(create_todo_tool(todos))  # 暴露 V2 Todo Tool
        skills = SkillCatalog(skills_root)  # 共用 Skill Catalog 文件来源
        tools.register(create_load_skill_tool(skills))  # 暴露 V2 按需加载 Tool
        memory = LockedMemoryManager(memory_store, model, memory_lock, auto_extract=auto_extract and is_lead)  # 共享数据，但每个 Agent 有独立 Manager
        for tool in memory.create_tools():  # 注册 V3 Memory Tool
            tools.register(tool)  # Memory 通过 V1 Tool Dispatcher 执行
        tasks = TaskScheduler(task_store)  # 每个 Agent 使用共享 TaskStore
        for tool in tasks.create_tools():  # V3 持久任务接口
            if is_lead or tool.name == "task_list":  # Worker 不允许绕过 Task Ownership 改状态
                tools.register(tool)  # Lead 可管理任务，Worker 只查询
        if not is_lead:  # 仅 Worker 需要自己的邮箱通信工具
            for tool in create_mailbox_tools(shared_mailbox, agent_id):  # 绑定 Worker 固定身份
                tools.register(tool)  # 注册 mail_send / mail_read
        else:  # Lead 有额外的团队和 V5 调度权限
            for tool in team.create_tools():  # V4 Team Manager Tool
                if tool.name != "team_run_ready":  # 阻塞版入口必须禁止暴露给 Lead
                    tools.register(tool)  # 保留 team_spawn、team_send、team_status
            for tool in create_event_tools(event_runtime, delivery, cron):  # 新增 V5 Tool 适配
                tools.register(tool)  # 通过 V1 注册后才能让模型使用
        hooks = HookManager()  # 每个 Agent 有独立的 Hook 管理器
        policy = IntegratedPermission(PermissionPolicy(), approve=approve)  # V1 原政策 + V5 审批
        hooks.register_pre_tool_use(policy.check)  # 所有 ToolCall 都经过单一权限入口
        context = ContextManager(model=model, workspace=workdir, todos=todos, skills=skills)  # V2 独立消息准备、Compaction 和 Prompt
        return Agent(model=model, tools=tools, hooks=hooks, context=context, memory=memory, tasks=tasks, max_steps=30)  # 返回真实可运行的 Agent Loop

    def make_worker(workdir: Path, agent_id: str, shared_mailbox: MailboxStore) -> Agent:  # V4 团队会通过该工厂构造每个 Worker
        return build_agent(workdir, agent_id, shared_mailbox, is_lead=False)  # Worker 不拥有 Lead 的调度 Tool

    team = TeamManager(task_store, mailbox, worktrees, make_worker)  # V4 持久化团队与任务认领
    jobs = BackgroundJobManager(JobStore(state / "jobs.json"), max_workers=max_workers)  # V5.1 后台执行器
    bus = EventBus(EventJournal(state / "events.jsonl"))  # V5.2 Event Queue + Journal
    dispatcher = EventDispatcher(bus)  # V5.2 在 Lead 线程分发完成事件
    notices = LeadNoticeStore(state / "lead_notices.json")  # V5.5 幂等持久 Lead 通知
    event_runtime = EventTeamRuntime(team, jobs, bus, dispatcher, notices)  # V5.5 Team → Job → Event
    cron = CronScheduler(CronStore(state / "cron.json"), jobs)  # V5.4 计划调度器
    event_runtime.register_cron_action(cron)  # 注册可重建的团队调度动作
    cursor = LeadCursorStore(state / "lead_cursor.json")  # V5.7 持久化 Lead 投递进度
    delivery = LeadDelivery(event_runtime, notices, cursor, lead=None)  # 创建尚未挂载 Lead 的事件投递器
    lead = build_agent(root, "lead", mailbox, is_lead=True, team=team, event_runtime=event_runtime, delivery=delivery, cron=cron)  # 真正的 V0 Lead Agent
    delivery.attach_lead(lead)  # 解决 Agent Tool 与通知投递之间的初始化引用环
    return DevPilotApp(lead=lead, delivery=delivery, runtime=event_runtime, jobs=jobs, cron=cron, team=team, state_dir=state, lead_lock=lead_lock)  # 统一应用入口
