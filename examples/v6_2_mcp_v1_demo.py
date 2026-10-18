"""V6.2 演示：真实 MCP Server 动态注册到 V1，调用时经过 Dispatcher 与权限 Hook。"""

from devpilot.core.message import ToolCall
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.integration import MCPToolIntegration
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.permissions import PermissionPolicy
from devpilot.tools.registry import ToolRegistry


def main() -> None:
    """用户运行示例时连接 V6.1 Demo Server，动态注册其工具并演示审批拒绝和允许。"""
    registry = ToolRegistry()
    approval_state = {"allowed": False}
    policy = MCPPermissionPolicy(PermissionPolicy(), approve=lambda name, args: approval_state["allowed"] and name == "mcp__demo__add")
    hooks = HookManager()
    hooks.register_pre_tool_use(policy.check)
    dispatcher = ToolDispatcher(registry, hooks)

    with MCPToolIntegration(registry) as integration:
        names = integration.add_local("demo", LocalServerConfig.demo(), allow_tools={"add", "echo"})
        print("动态发现并注册：", names)
        call = ToolCall(id="mcp-v62-call", name="mcp__demo__add", arguments={"a": 3, "b": 4})
        print("未授权：", dispatcher.execute(call))
        approval_state["allowed"] = True
        print("已授权：", dispatcher.execute(call))
        print("参数非法：", dispatcher.execute(ToolCall(id="bad-args", name="mcp__demo__add", arguments={"a": "wrong", "b": 4})))


if __name__ == "__main__":
    main()
