from collections.abc import Callable

from devpilot.core.message import ToolCall

class PermissionPolicy:

    DENY_COMMANDS = (
        "rm -rf /",
        "sudo ",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
    )

    SAFE_COMMANDS = (
        "pytest",
        "python -m pytest",
        "git status",
        "git diff",
        "git log",
        "pwd",
        "ls",
        "dir",
    )

    def __init__(self,confirmer: Callable[[str],bool,] | None = None,):
        self.confirmer=(confirmer or self._default_confirmer)

    def check(self,tool_call: ToolCall)->str|None:
        return None
        '''
        name=tool_call.name
        # ----------
        # READ ONLY
        # ----------
        if name in{
            "read_file",
            "glob",
            "todo_write",
            "load_skill",
            "memory_write",
            "memory_search",
            "task_create",
            "task_update",
            "task_list",
        }:
            return None
        
        # ----------
        # WRITE TOOL
        # ----------
        if name in {"write_file","edit_file",}:
            allowed = self.confirmer(
                f"Allow {name} "
                f"{tool_call.arguments}? "
            )
            if allowed:
                return None
            return ("Permission denied " "by user")
        # ----------
        # BASH
        # ----------
        if name == "bash":
            command = (tool_call.arguments.get("command"))
            if not isinstance(command,str,):
                return (
                    "Permission denied: "
                    "command must be string"
                )
            normalized = (command.strip().lower())
            for pattern in self.DENY_COMMANDS:
                if pattern in normalized:
                    return (
                        "Permission denied: "
                        f"dangerous command "
                        f"'{pattern}'"
                    )
            for safe in (self.SAFE_COMMANDS):
                if normalized.startswith(safe):
                    return None

            allowed = self.confirmer(
                f"Allow bash command "
                f"'{command}'? "
            )
            if allowed:
                return None

            return (
                "Permission denied "
                "by user"
            )
        # ----------
        # UNKNOWN
        # ----------
        return (
            f"Permission denied: "
            f"tool '{name}' "
            f"is not allowed"
        )
    '''
    @staticmethod
    def _default_confirmer(prompt: str,) -> bool:
        answer = input(f"{prompt}[y/N]: ")
        return (answer.strip().lower()in {"y", "yes"})