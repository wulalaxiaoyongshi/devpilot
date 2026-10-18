"""V6.2：将 MCP 工具默认拒绝并交由宿主逐次授权的权限适配层。"""

from collections.abc import Callable
from typing import Any


class MCPPermissionPolicy:
    """在 V1 原权限策略外围新增 MCP 专属审批，普通工具仍交回 V1 判断。"""

    def __init__(self, base_policy: Any, approve: Callable[[str, dict], bool] | None = None):
        """Factory 初始化 V1 Hook 时调用，保存原有 PermissionPolicy 和可选审批函数。"""
        self.base_policy = base_policy
        self.approve = approve

    def check(self, call: Any) -> str | None:
        """每次 MCP ToolCall 进入 V1 PreToolUse Hook 时调用，未获明确批准便拒绝执行。"""
        if not call.name.startswith("mcp__"):
            return self.base_policy.check(call)
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if self.approve is not None and self.approve(call.name, arguments):
            return None
        return f"MCP tool approval required: {call.name}"
