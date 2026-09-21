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
    return current  # 兜底返回当前目录

# 自动向上锚定项目顶层根目录
workspace = detect_project_root(Path.cwd())


# ============================
# Model
# ============================

model = DeepSeekModel()


# ============================
# Tools
# ============================

tools = create_builtin_registry(
    workspaceF
)


# ============================
# Todo
# ============================

todos = TodoManager()

tools.register(
    create_todo_tool(
        todos
    )
)


# ============================
# Skills
# ============================

skills = SkillCatalog(workspace / "skills")

tools.register(
    create_load_skill_tool(
        skills
    )
)


# ============================
# Hooks + Permission
# ============================

hooks = HookManager()

permission = PermissionPolicy()

hooks.register_pre_tool_use(
    permission.check
)


# ============================
# Context
# ============================

context = ContextManager(
    model=model,
    workspace=workspace,
    todos=todos,
    skills=skills,
)


# ============================
# Agent
# ============================

agent = Agent(
    model=model,
    tools=tools,
    hooks=hooks,
    context=context,
)


# ============================
# Run
# ============================

result = agent.run("先把skills加载出来，然后数一下有几个skills")


print(result)