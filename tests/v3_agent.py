from pathlib import Path

from devpilot.context.manager import (
    ContextManager,
)
from devpilot.context.skills import (
    SkillCatalog,
    create_load_skill_tool,
)
from devpilot.context.todo import (
    TodoManager,
    create_todo_tool,
)
from devpilot.core.agent import Agent
from devpilot.core.model import (
    DeepSeekModel,
)
from devpilot.memory.manager import (
    MemoryManager,
)
from devpilot.memory.store import (
    MemoryStore,
)
from devpilot.tasks.scheduler import (
    TaskScheduler,
)
from devpilot.tasks.store import (
    TaskStore,
)
from devpilot.tools.builtin.factory import (
    create_builtin_registry,
)
from devpilot.tools.hooks import (
    HookManager,
)
from devpilot.tools.permissions import (
    PermissionPolicy,
)

def detect_project_root(start: Path) -> Path:
    current = start.resolve()
    # 遍历当前目录以及它的所有上级目录：[当前目录, 父目录, 祖父目录...]
    for parent in [current, *current.parents]:
        # 只要找到了 .git 目录或者 skills 目录，就认定为项目顶层
        if (parent / ".git").exists() or (parent / "skills").is_dir():
            return parent
    return current

workspace = detect_project_root(Path.cwd())


# =========================
# Model
# =========================

model = DeepSeekModel()


# =========================
# V1 Tool Runtime
# =========================

tools = create_builtin_registry(workspace)

hooks = HookManager()

permission = PermissionPolicy()

hooks.register_pre_tool_use(permission.check)


# =========================
# V2 Context Runtime
# =========================

todos = TodoManager()

skills = SkillCatalog(
    workspace / "skills"
)

tools.register(
    create_todo_tool(
        todos
    )
)

tools.register(
    create_load_skill_tool(
        skills
    )
)

context = ContextManager(
    model=model,
    workspace=workspace,
    todos=todos,
    skills=skills,
)


# =========================
# V3 Memory Runtime
# =========================

memory_store = MemoryStore(
    workspace
    / ".devpilot"
    / "memory.json"
)

memory = MemoryManager(
    store=memory_store,
    model=model,
)

for tool in memory.create_tools():

    tools.register(tool)


# =========================
# V3 Task Runtime
# =========================

task_store = TaskStore(
    workspace
    / ".devpilot"
    / "tasks.json"
)

tasks = TaskScheduler(
    store=task_store
)

for tool in tasks.create_tools():

    tools.register(tool)


# =========================
# Agent
# =========================

agent = Agent(
    model=model,
    tools=tools,
    hooks=hooks,
    context=context,
    memory=memory,
    tasks=tasks,
)


result = agent.run(
    "查看当前项目状态。"
    "如果这是一个长期任务，"
    "用 persistent task graph 管理；"
    "需要长期保存的重要信息"
    "写入 memory。"
)


print(result)