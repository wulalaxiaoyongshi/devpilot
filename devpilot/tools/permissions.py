"""V9.1：恢复 V1 工具默认拒绝的权限检查，防止未授权写文件和运行命令。"""

from collections.abc import Callable

from devpilot.core.message import ToolCall


class PermissionPolicy:
    """检查普通工具的权限，并将文件写入和非只读命令交给可信确认函数。"""

    DENY_COMMANDS = ("rm -rf /", "sudo ", "shutdown", "reboot", "mkfs", "dd if=")
    SAFE_COMMANDS = ("pytest", "python -m pytest", "git status", "git diff", "git log", "pwd", "ls", "dir")
    ALLOWED_TOOLS = frozenset({
        "read_file", "glob", "todo_write", "load_skill", "memory_write", "memory_search",
        "task_create", "task_update", "task_list", "mail_send", "mail_read",
        "team_send", "team_status", "job_status", "cron_list", "event_poll",
    })

    def __init__(self, confirmer: Callable[[str], bool] | None = None):
        """接收可信确认函数，未传入时只通过终端显式询问用户。"""
        self.confirmer = confirmer or self._default_confirmer

    @classmethod
    def hard_denial(cls, command: object) -> str | None:
        """对无效或明确危险的命令直接拒绝，不让普通确认绕过硬性禁止规则。"""
        if not isinstance(command, str) or not command.strip():
            return "Permission denied: command must be nonempty string"
        normalized = command.strip().lower()
        for forbidden in cls.DENY_COMMANDS:
            if forbidden in normalized:
                return f"Permission denied: dangerous command '{forbidden}'"
        return None

    @classmethod
    def _safe_bash(cls, command: str) -> bool:
        """只有不含 Shell 控制符且准确匹配白名单前缀的命令才能免审批执行。"""
        if any(marker in command for marker in (";", "|", "&", ">", "<", "\n", "\r", "`", "$(")):
            return False
        normalized = command.strip().lower()
        return any(normalized == safe or normalized.startswith(safe + " ") for safe in cls.SAFE_COMMANDS)

    def check(self, tool_call: ToolCall) -> str | None:
        """只直接允许已知低风险工具，其他操作遵循显式批准或拒绝规则。"""
        name = tool_call.name
        if name in self.ALLOWED_TOOLS:
            return None
        if name in {"write_file", "edit_file"}:
            if self.confirmer(f"Allow {name} {tool_call.arguments}? "):
                return None
            return "Permission denied by user"
        if name == "bash":
            command = tool_call.arguments.get("command") if isinstance(tool_call.arguments, dict) else None
            denied = self.hard_denial(command)
            if denied is not None:
                return denied
            if self._safe_bash(command):
                return None
            if self.confirmer(f"Allow bash command {command!r}? "):
                return None
            return "Permission denied by user"
        return f"Permission denied: tool '{name}' is not allowed"

    @staticmethod
    def _default_confirmer(prompt: str) -> bool:
        """使用终端确认敏感操作，输入不可用时默认拒绝而不是放行。"""
        try:
            return input(f"{prompt}[y/N]: ").strip().lower() in {"y", "yes"}
        except (EOFError, OSError):
            return False
