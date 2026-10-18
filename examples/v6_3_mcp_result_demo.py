"""V6.3：让真正的 V1 ToolDispatcher 调用本地 MCP Server 并展示分类后的结果。"""

from devpilot.core.message import ToolCall
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.integration import MCPToolIntegration
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.permissions import PermissionPolicy
from devpilot.tools.registry import ToolRegistry


def main() -> None:
    """启动本地 Server，展示参数校验、成功结果、业务错误和权限拒绝的完整执行链。"""
    registry = ToolRegistry()
    approved = {"mcp__demo__add", "mcp__demo__fail"}
    policy = MCPPermissionPolicy(PermissionPolicy(), approve=lambda name, arguments: name in approved)
    hooks = HookManager()
    hooks.register_pre_tool_use(policy.check)
    dispatcher = ToolDispatcher(registry, hooks)

    with MCPToolIntegration(registry) as integration:
        print("已注册工具：", integration.add_local("demo", LocalServerConfig.demo(), allow_tools={"add", "echo", "fail"}))
        success = ToolCall(id="success", name="mcp__demo__add", arguments={"a": 3, "b": 4})
        invalid = ToolCall(id="invalid", name="mcp__demo__add", arguments={"a": "wrong", "b": 4})
        business_failure = ToolCall(id="business", name="mcp__demo__fail", arguments={"message": "expected failure"})
        denied = ToolCall(id="denied", name="mcp__demo__echo", arguments={"message": "not approved"})
        print("成功：", dispatcher.execute(success))
        print("参数错误：", dispatcher.execute(invalid))
        print("Server 业务错误：", dispatcher.execute(business_failure))
        print("权限拒绝：", dispatcher.execute(denied))


if __name__ == "__main__":
    main()
