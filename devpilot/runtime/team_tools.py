"""V9.3：为真实团队演示建立 Lead 与 Worker 分离的最小权限工具集合。"""

from pathlib import Path
from typing import Any

from devpilot.runtime.analysis_tools import create_analysis_registry
from devpilot.tools.base import Tool
from devpilot.tools.registry import ToolRegistry


LEAD_TEAM_TOOLS = ("team_spawn", "team_submit_ready", "team_status", "job_status", "event_poll")
WORKER_READ_TOOLS = ("glob", "read_file", "todo_write", "task_list", "memory_search", "mail_send", "mail_read")


def create_team_lead_registry(workspace: Path, source: ToolRegistry, *, allowed_worker_ids: tuple[str, ...]) -> ToolRegistry:
    """只把读取、记忆、任务规划和团队提交工具提供给 Lead，不开放文件修改或 Shell。"""
    if not allowed_worker_ids or any(not name.isidentifier() or not name.isascii() for name in allowed_worker_ids):
        raise ValueError("Team mode requires simple ASCII worker IDs")
    selected = create_analysis_registry(workspace, source)
    for name in LEAD_TEAM_TOOLS:
        tool = source.get(name)
        if tool is None:
            raise RuntimeError(f"Team mode requires tool: {name}")
        if name == "team_spawn":
            def spawn_checked(arguments: dict[str, Any], target_tool: Tool = tool) -> str:
                """只能让模型创建宿主预先批准的 Worker 身份，避免任意 Git 分支和 Worktree 名称。"""
                if arguments.get("agent_id") not in allowed_worker_ids:
                    return "Error: worker ID is not approved for this session"
                return target_tool.handler(arguments)
            selected.register(Tool(tool.name, tool.description, tool.parameters, spawn_checked))
        else:
            selected.register(tool)
    return selected


def _allowed_worker_path(workspace: Path, name: Any, allowed: frozenset[str]) -> Path | None:
    """只允许 Worker 写入宿主事先批准的仓库内 Python 文件，拒绝链接和路径跳转。"""
    if not isinstance(name, str) or name not in allowed or not name or "\x00" in name:
        return None
    path = Path(name)
    if path.is_absolute() or path.suffix.lower() != ".py" or any(part in (".", "..") or part.startswith(".") for part in path.parts):
        return None
    target = workspace / path
    if any(part.is_symlink() for part in (target, *target.parents) if part != workspace and workspace in part.parents):
        return None
    try:
        target.resolve().relative_to(workspace)
    except (ValueError, OSError, RuntimeError):
        return None
    return target


def create_team_worker_registry(workspace: Path, source: ToolRegistry, *, allowed_write_files: tuple[str, ...]) -> ToolRegistry:
    """Worker 只能读取安全源码并写入明确列出的 Python 文件，且不能运行 Shell 或管理其他任务。"""
    root = workspace.resolve()
    allowlist = frozenset(allowed_write_files)
    if not allowlist or any(_allowed_worker_path(root, name, allowlist) is None for name in allowlist):
        raise ValueError("Team mode requires a valid nonempty worker write allowlist")
    shadow = ToolRegistry()
    for name in source.names():
        shadow.register(source.get(name))
    if shadow.get("task_create") is None:
        shadow.register(Tool("task_create", "internal unused placeholder", {"type": "object"}, lambda _: "Error: forbidden"))
    safe_reads = create_analysis_registry(root, shadow)
    selected = ToolRegistry()
    for name in WORKER_READ_TOOLS:
        tool = (source if name in {"todo_write", "task_list", "memory_search", "mail_send", "mail_read"} else safe_reads).get(name)
        if tool is None:
            raise RuntimeError(f"Worker tool is missing: {name}")
        selected.register(tool)
    raw_write = source.get("write_file")
    if raw_write is None:
        raise RuntimeError("Worker tool is missing: write_file")

    def write_checked(arguments: dict[str, Any]) -> str:
        """在调用 V1 原始写文件 Handler 之前检查文件白名单、文本大小和路径边界。"""
        target = _allowed_worker_path(root, arguments.get("path"), allowlist)
        content = arguments.get("content")
        if target is None or not isinstance(content, str) or len(content.encode("utf-8")) > 20_000:
            return "Error: worker write requires an allowed Python path and UTF-8 content <= 20000 bytes"
        return raw_write.handler(arguments)

    selected.register(Tool(raw_write.name, raw_write.description, raw_write.parameters, write_checked))
    return selected
