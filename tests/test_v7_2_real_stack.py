"""仅在存在用户真正 V1/V6 环境时运行 Workflow → V1 Dispatcher → MCP 的集成测试。"""

from uuid import uuid4

import pytest

from devpilot.workflow import (
    ActionRegistry,
    StepDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRuntimeAdapters,
    WorkflowStore,
    normalize_mcp_v1_text,
)


def test_real_v1_dispatcher_and_v6_dynamic_tool_with_workflow(tmp_path):
    """V7 Workflow 调用真实 V1 Dispatcher 的 MCP 工具，并验证未经批准时远端绝不执行。"""
    pytest.importorskip("devpilot.tools.dispatcher", reason="需要用户自己的 V1 ToolDispatcher")
    pytest.importorskip("devpilot.mcp.config", reason="需要原工程 V6.1 MCP Client")
    pytest.importorskip("mcp", reason="需要官方 MCP SDK")
    from devpilot.core.message import ToolCall
    from devpilot.mcp.config import LocalServerConfig
    from devpilot.mcp.integration import MCPToolIntegration
    from devpilot.mcp.permission import MCPPermissionPolicy
    from devpilot.tools.dispatcher import ToolDispatcher
    from devpilot.tools.hooks import HookManager
    from devpilot.tools.registry import ToolRegistry

    registry = ToolRegistry()
    hooks = HookManager()
    permission = {"allow": False}

    class DefaultAllowLocal:
        """测试场景仅为 MCP，普通 V1 权限由测试替身默认放行。"""

        def check(self, call):
            """在测试中把非 MCP 调用交回已有的基础策略。"""
            return None

    hooks.register_pre_tool_use(MCPPermissionPolicy(DefaultAllowLocal(), approve=lambda name, args: permission["allow"]).check)
    dispatcher = ToolDispatcher(registry, hooks)
    observed = []

    def dispatch(name, args):
        """生成真正的 V1 ToolCall，并把 V6 明确标记的文本结果转成结构化状态。"""
        result = dispatcher.execute(ToolCall(id=uuid4().hex, name=name, arguments=args))
        observed.append(result)
        return normalize_mcp_v1_text(result)

    actions = ActionRegistry()
    adapters = WorkflowRuntimeAdapters(actions)
    adapters.register_v6_mcp(dispatch, allowed_tools={"mcp__demo__add"})
    definition = WorkflowDefinition("real-mcp", "Real MCP", (StepDefinition("add", action="v6.mcp_tool", params={"tool": "mcp__demo__add", "arguments": {"a": 7, "b": 8}}),))
    engine = WorkflowEngine(WorkflowStore(tmp_path / "workflows"), actions)
    with MCPToolIntegration(registry) as integration:
        integration.add_local("demo", LocalServerConfig.demo(), allow_tools={"add"})
        denied = engine.run_until_blocked(engine.start(definition).id)
        assert denied.status == "failed"
        assert "approval" in observed[-1].lower() or "denied" in observed[-1].lower()
        permission["allow"] = True
        approved = engine.run_until_blocked(engine.start(definition).id)
        assert approved.status == "completed"
        assert "15" in approved.steps["add"].output["content"]
