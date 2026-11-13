"""V9.1：将 V1、V5 和 MCP 的权限规则串成统一的最小授权入口。"""

from collections.abc import Callable
from typing import Any

from devpilot.tools.permissions import PermissionPolicy


APPROVAL_REQUIRED = frozenset({"team_spawn", "team_submit_ready", "job_cancel", "cron_create"})
READ_ONLY = frozenset({"job_status", "cron_list", "event_poll", "team_status"})


class IntegratedPermission:
    """针对 V5 高风险操作要求额外审批，并让其他工具继续接受 V1 和 MCP 检查。"""

    def __init__(self, base_policy: Any, approve: Callable[[str, dict], bool] | None = None):
        """绑定现有权限策略与可信宿主审批函数。"""
        self.base_policy = base_policy
        self.approve = approve

    def check(self, call: Any) -> str | None:
        """先拒绝危险命令，再对 MCP、普通工具与 V5 调度分别实施授权。"""
        name = call.name
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if name == "bash":
            denied = PermissionPolicy.hard_denial(arguments.get("command"))
            if denied is not None:
                return denied
        if name.startswith("mcp__"):
            return self.base_policy.check(call)
        if name in READ_ONLY:
            return None
        if name in APPROVAL_REQUIRED:
            if self.approve is not None and self.approve(name, arguments):
                return None
            return f"Approval required for {name}"
        if name in {"write_file", "edit_file", "bash"}:
            if self.approve is not None and self.approve(name, arguments):
                return None
        return self.base_policy.check(call)
