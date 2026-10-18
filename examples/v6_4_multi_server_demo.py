"""V6.4：使用两个真实本地 MCP Server 通过原来的 V1 Dispatcher 调用不同工具。"""

from devpilot.core.message import ToolCall
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.multi_server import MCPMultiServerManager
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.mcp.server_config import MCPServerDefinition
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.permissions import PermissionPolicy
from devpilot.tools.registry import ToolRegistry


def main() -> None:
    """演示两台 Server 连接、权限检查、独立工具调用、断连和恢复。"""
    registry = ToolRegistry()
    allowed = {"mcp__math__add", "mcp__text__echo"}
    policy = MCPPermissionPolicy(PermissionPolicy(), approve=lambda name, args: name in allowed)
    hooks = HookManager()
    hooks.register_pre_tool_use(policy.check)
    dispatcher = ToolDispatcher(registry, hooks)
    local = LocalServerConfig.demo()
    servers = [
        MCPServerDefinition("math", local, allow_tools=frozenset({"add"}), timeout_seconds=10),
        MCPServerDefinition("text", local, allow_tools=frozenset({"echo"}), timeout_seconds=10),
    ]
    with MCPMultiServerManager(registry, servers) as manager:
        print("连接结果：", manager.connect_all(strict=True))
        math_call = ToolCall(id="math-1", name="mcp__math__add", arguments={"a": 2, "b": 5})
        text_call = ToolCall(id="text-1", name="mcp__text__echo", arguments={"message": "hello"})
        print("Math：", dispatcher.execute(math_call))
        print("Text：", dispatcher.execute(text_call))
        original_handler = registry.get("mcp__math__add").handler
        print("Math 停止后状态：", manager.disconnect("math"))
        print("Math 停止后调用：", dispatcher.execute(math_call))
        print("Text 不受影响：", dispatcher.execute(text_call))
        print("Math 重连后状态：", manager.connect("math"))
        assert registry.get("mcp__math__add").handler is original_handler
        print("Math 重连后调用：", dispatcher.execute(math_call))
        print("Math 健康检查：", manager.check_health("math"))
    print("全部 MCP Server 已关闭")


if __name__ == "__main__":
    main()
