"""V9.2：在不影响原 Agent 字符串协议的前提下，提供结构化工具调用结果。"""

from dataclasses import dataclass
from typing import Literal

from devpilot.core.message import ToolCall
from devpilot.tools.hooks import HookManager
from devpilot.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolExecution:
    """区分权限拒绝、工具不存在、执行异常和仅有文本返回这四种事实。"""

    name: str
    status: Literal["denied", "unknown_tool", "exception", "returned"]
    content: str

    @property
    def ok(self) -> bool | None:
        """只有确定未执行成功时返回 False，普通旧工具文本结果保留为未知。"""
        return False if self.status != "returned" else None


class ToolDispatcher:
    """继续提供旧版 execute，同时允许宿主检查真实执行阶段的结构化状态。"""

    def __init__(self, registry: ToolRegistry, hooks: HookManager):
        """绑定真实的工具注册表和原有权限 Hook。"""
        self.registry = registry
        self.hooks = hooks

    def execute(self, tool_call: ToolCall) -> str:
        """保持 V0 Agent 依赖的字符串 ToolResult 接口不变。"""
        return self.execute_detailed(tool_call).content

    def execute_detailed(self, tool_call: ToolCall) -> ToolExecution:
        """不通过解析 Error 前缀猜测成功，而是记录是否真正调用到了 Handler。"""
        tool = self.registry.get(tool_call.name)
        if tool is None:
            return ToolExecution(tool_call.name, "unknown_tool", f"Error: unknown tool '{tool_call.name}'")
        blocked = self.hooks.run_pre_tool_use(tool_call)
        if blocked is not None:
            return ToolExecution(tool_call.name, "denied", blocked)
        try:
            content = tool.handler(tool_call.arguments)
            if not isinstance(content, str):
                raise TypeError("Tool handler must return a string")
        except Exception as error:
            return ToolExecution(tool_call.name, "exception", f"{type(error).__name__}: {error}")
        self.hooks.run_post_tool_use(tool_call, content)
        return ToolExecution(tool_call.name, "returned", content)
