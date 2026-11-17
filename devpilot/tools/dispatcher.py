"""V1/V10.0：统一工具调用入口，新增可核实的结构化调用状态。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from devpilot.core.message import ToolCall
from devpilot.tools.hooks import HookManager
from devpilot.tools.registry import ToolRegistry

ToolStatus = Literal["returned", "denied", "unknown_tool", "error"]


@dataclass(frozen=True)
class ToolExecution:
    """区分成功返回、权限拒绝、工具缺失和工具异常；不据返回文本推断业务成功。"""
    status: ToolStatus
    content: str


class ToolDispatcher:
    """所有工具必须先由 PreToolUse 审批，再调用 Handler 和 PostToolUse。"""

    def __init__(self, registry: ToolRegistry, hooks: HookManager):
        """绑定原 V1 工具表与安全 Hook。"""
        self.registry = registry
        self.hooks = hooks

    def execute_detailed(self, tool_call: ToolCall) -> ToolExecution:
        """执行一次真实 ToolCall 并输出机器可判断的状态。"""
        tool = self.registry.get(tool_call.name)
        if tool is None:
            return ToolExecution("unknown_tool", f"Error: unknown tool '{tool_call.name}'")
        blocked = self.hooks.run_pre_tool_use(tool_call)
        if blocked is not None:
            return ToolExecution("denied", str(blocked))
        try:
            result = tool.handler(tool_call.arguments)
        except Exception as error:
            return ToolExecution("error", f"{type(error).__name__}: {error}")
        if not isinstance(result, str):
            return ToolExecution("error", "TypeError: Tool handler must return str")
        try:
            self.hooks.run_post_tool_use(tool_call, result)
        except Exception as error:
            return ToolExecution("error", f"PostToolUse {type(error).__name__}: {error}")
        return ToolExecution("returned", result)

    def execute(self, tool_call: ToolCall) -> str:
        """保持 V0 Agent 的字符串调用接口，交给结构化分发器执行一次。"""
        return self.execute_detailed(tool_call).content
