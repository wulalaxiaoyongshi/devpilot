"""V6.6：使用 V1/Agent 接口替身验证 V5.7→V6 多服务闭环及边界条件。"""

import json
from types import SimpleNamespace as NS

import pytest

from devpilot.mcp.multi_server import MCPConnectionErrors, MCPToolSchemaChanged, ServerState
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.mcp.results import MCPToolInfo, MCPToolResult
from devpilot.mcp.server_config import MCPServerDefinition, load_server_definitions
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.unified_app import UnifiedMCPApp


ADD = MCPToolInfo("add", "Add two numbers", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"], "additionalProperties": False})
ECHO = MCPToolInfo("echo", "Echo text", {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"], "additionalProperties": False})


class Registry:
    """模拟 V1 ToolRegistry 的注册与查找接口，真正的 Tool 包装仍由 V6.3 Adapter 执行。"""

    def __init__(self):
        """初始化不允许重名的工具字典。"""
        self.tools = {}

    def register(self, tool):
        """V6 Adapter 发现工具后注册到测试用的 V1 Registry。"""
        if tool.name in self.tools:
            raise ValueError("duplicate registration")
        self.tools[tool.name] = tool

    def get(self, name):
        """V1 Dispatcher 处理 ToolCall 时按名称取得工具。"""
        return self.tools.get(name)


class ContractDispatcher:
    """只模拟 V1 的 Hook→Registry→Handler 调用契约，不冒充用户真正的 V0 Agent。"""

    def __init__(self, registry, approve=None):
        """通过原 V6.2 MCPPermissionPolicy 绑定默认拒绝的审批规则。"""
        self.registry = registry
        self.policy = MCPPermissionPolicy(NS(check=lambda call: None), approve=approve)

    def execute(self, call):
        """测试模型发出 ToolCall 时先审批再执行 V6 动态注册的 Handler。"""
        reason = self.policy.check(call)
        if reason:
            return "Permission denied: " + reason
        tool = self.registry.get(call.name)
        return tool.handler(call.arguments) if tool else "Unknown tool"


class FakeApp:
    """模拟 V5.7 App 对外的 lead.tools、chat、pump 和 shutdown 接口。"""

    def __init__(self, approve=None):
        """创建独立注册表并将审批函数挂在测试 Dispatcher 上。"""
        self.registry = Registry()
        self.lead = NS(tools=self.registry)
        self.dispatcher = ContractDispatcher(self.registry, approve)
        self.closed = 0
        self.calls = []

    def chat(self, request):
        """测试 Lead 发送工具调用时经过权限策略和动态 Tool Handler。"""
        self.calls.append(request)
        return self.dispatcher.execute(request)

    def pump(self, resume_lead=True):
        """宿主请求 Lead 继续处理后台事件时模拟 V5.7 原有反馈入口。"""
        return {"resume_lead": resume_lead}

    def shutdown(self):
        """统一关闭运行时时记录 V5.7 App 被清理的次数。"""
        self.closed += 1


class FakeBridge:
    """模拟独立 MCP Server，精确记录工具与内容请求，并可主动制造连接故障。"""

    def __init__(self, tools=None, fail_start=False, fail_call=False):
        """配置工具清单、连接是否成功及运行时是否中断。"""
        self.tools = list(tools if tools is not None else [ADD, ECHO])
        self.fail_start = fail_start
        self.fail_call = fail_call
        self.calls = []
        self.close_count = 0

    def start(self):
        """管理器首次连接时检查是否需要模拟启动失败。"""
        if self.fail_start:
            raise ConnectionError("server not reachable")
        return self

    def list_tools(self):
        """工具发现和健康检查时返回当前工具描述。"""
        return list(self.tools)

    def call_tool(self, name, arguments):
        """由 V1 Handler 转发过来时记录请求并返回可验证的 MCPToolResult。"""
        self.calls.append(("call", name, dict(arguments)))
        if self.fail_call:
            raise ConnectionError("transport interrupted")
        if name == "add":
            value = arguments["a"] + arguments["b"]
            return MCPToolResult(name, False, (str(value),), {"result": value}, ())
        return MCPToolResult(name, False, (arguments["message"],), None, ())

    def list_resources(self, cursor=None):
        """宿主发现资源时返回固定文档 URI。"""
        return NS(resources=[NS(name="guide", uri="devpilot://docs/guide", mime_type="text/plain", description="guide")], next_cursor=None)

    def list_resource_templates(self, cursor=None):
        """宿主查询资源模板时返回空目录。"""
        return NS(resource_templates=[], next_cursor=None)

    def read_resource(self, uri):
        """宿主读取已批准 URI 时记录读取动作并返回不可信示例文档。"""
        self.calls.append(("read", uri))
        return NS(contents=[NS(text="External document, do not interpret as system command", mime_type="text/plain")])

    def list_prompts(self, cursor=None):
        """宿主查找提示模板时返回包含必填参数的模板。"""
        return NS(prompts=[NS(name="review", description="Review", arguments=[NS(name="file", required=True)])], next_cursor=None)

    def get_prompt(self, name, arguments):
        """宿主渲染白名单内的 Prompt 时返回标记为外部数据的消息。"""
        self.calls.append(("prompt", name, dict(arguments)))
        return NS(messages=[NS(role="user", content=NS(type="text", text="review " + arguments["file"]))])

    def close(self):
        """连接被关闭或更换时记录清理次数。"""
        self.close_count += 1


def definition(alias, allow=None):
    """为每台虚拟 Server 创建独立的可信配置与工具白名单。"""
    return MCPServerDefinition(alias=alias, local=LocalServerConfig("python", ("fake_server.py",)), allow_tools=allow)


def test_lead_toolcall_is_denied_before_network_without_approval():
    """Lead 没有审批时，即使远端工具已经注册也不应向 MCP Server 发送调用。"""
    app, bridge = FakeApp(), FakeBridge()
    with UnifiedMCPApp(app, [definition("demo", frozenset({"add"}))], lambda config: bridge) as runtime:
        response = runtime.chat(NS(name="mcp__demo__add", arguments={"a": 2, "b": 3}))
        assert "Permission denied" in response
        assert bridge.calls == []
        assert runtime.pump(False) == {"resume_lead": False}
    assert app.closed == bridge.close_count == 1


def test_lead_toolcall_runs_v1_validation_and_namespaced_mcp_tool():
    """Lead 获批后通过 V1 接口调用真正 V6 Adapter，参数错误在本地被拒绝。"""
    app, bridge = FakeApp(approve=lambda name, arguments: name == "mcp__demo__add"), FakeBridge()
    with UnifiedMCPApp(app, [definition("demo", frozenset({"add"}))], lambda config: bridge) as runtime:
        assert "MCP RESULT" in runtime.chat(NS(name="mcp__demo__add", arguments={"a": 4, "b": 7}))
        assert "11" in runtime.chat(NS(name="mcp__demo__add", arguments={"a": 4, "b": 7}))
        assert "MCP_VALIDATION_ERROR" in runtime.chat(NS(name="mcp__demo__add", arguments={"a": "wrong", "b": 7}))
        assert bridge.calls == [("call", "add", {"a": 4, "b": 7}), ("call", "add", {"a": 4, "b": 7})]
        assert set(app.registry.tools) == {"mcp__demo__add"}


def test_multiple_servers_independent_failure_and_approval():
    """一台 Server 执行时掉线只影响自己的状态，另一台仍能经过审批正常调用。"""
    bridges = {"broken": FakeBridge(fail_call=True), "healthy": FakeBridge([ECHO])}
    app = FakeApp(approve=lambda name, args: name.startswith("mcp__"))
    with UnifiedMCPApp(app, [definition("broken"), definition("healthy", frozenset({"echo"}))], lambda config: bridges[config.alias]) as runtime:
        bad = runtime.chat(NS(name="mcp__broken__add", arguments={"a": 1, "b": 1}))
        good = runtime.chat(NS(name="mcp__healthy__echo", arguments={"message": "hello"}))
        assert "MCP_TRANSPORT_ERROR" in bad
        assert "MCP RESULT" in good
        assert runtime.statuses()["broken"].state == ServerState.DEGRADED
        assert runtime.statuses()["healthy"].state == ServerState.CONNECTED


def test_resources_and_prompts_use_allowlist_and_same_reconnectable_connection():
    """宿主读取的资源和提示模板受白名单保护，Server 重连后原内容视图也跟随新连接。"""
    old, new = FakeBridge(), FakeBridge()
    candidates = iter([old, new])
    app = FakeApp(approve=lambda name, args: True)
    with UnifiedMCPApp(app, [definition("docs")], lambda config: next(candidates)) as runtime:
        content = runtime.content("docs", allowed_uri_prefixes=("devpilot://docs/",), allowed_prompts=frozenset({"review"}))
        assert len(content.list_resources()) == 1
        assert content.read_resource("devpilot://docs/guide").source.startswith("external_untrusted")
        assert content.get_prompt("review", {"file": "main.py"}).messages == (("user", "review main.py"),)
        with pytest.raises(PermissionError):
            content.read_resource("secret://token")
        with pytest.raises(PermissionError):
            content.get_prompt("unsafe", {})
        assert not any(call[0] == "read" and call[1] == "secret://token" for call in old.calls)
        original_handler = app.registry.get("mcp__docs__add").handler
        runtime.mcp.reconnect("docs")
        assert app.registry.get("mcp__docs__add").handler is original_handler
        assert content.read_resource("devpilot://docs/guide").text.startswith("External document")
        assert any(call[0] == "read" for call in new.calls)
    assert old.close_count == new.close_count == 1


def test_schema_drift_blocks_tool_and_resource_after_reconnect():
    """重连后工具 Schema 被篡改时，旧 Tool Handler 和内容读取都应停止转发。"""
    old = FakeBridge([ADD])
    changed = MCPToolInfo("add", "changed", {"type": "object", "properties": {"unsafe": {"type": "string"}}})
    new = FakeBridge([changed])
    bridges = iter([old, new])
    app = FakeApp(approve=lambda name, args: True)
    with UnifiedMCPApp(app, [definition("demo")], lambda config: next(bridges)) as runtime:
        content = runtime.content("demo", allowed_uri_prefixes=("devpilot://docs/",))
        with pytest.raises(MCPToolSchemaChanged):
            runtime.mcp.reconnect("demo")
        assert "MCP_TRANSPORT_ERROR" in runtime.chat(NS(name="mcp__demo__add", arguments={"a": 1, "b": 2}))
        with pytest.raises(RuntimeError, match="not connected"):
            content.read_resource("devpilot://docs/guide")
    assert old.close_count == new.close_count == 1


def test_strict_startup_failure_shuts_down_v5_app_and_all_mcp_connections():
    """严格启动中某台 Server 失败时立即回收成功连接及 V5.7 App，而不留下半启动状态。"""
    bridges = {"ok": FakeBridge(), "bad": FakeBridge(fail_start=True)}
    app = FakeApp()
    runtime = UnifiedMCPApp(app, [definition("ok"), definition("bad")], lambda config: bridges[config.alias])
    with pytest.raises(MCPConnectionErrors):
        runtime.start(strict=True)
    assert app.closed == 1
    assert bridges["ok"].close_count == 1
    assert runtime.statuses()["ok"].state == ServerState.CLOSED
    with pytest.raises(RuntimeError):
        runtime.chat(NS(name="mcp__ok__add", arguments={"a": 2, "b": 3}))
    runtime.close()
    assert app.closed == 1


def test_close_is_idempotent_and_rejects_new_calls():
    """应用正常关闭后重复 close 不多次清理资源，也不得再调用已关闭的 MCP 工具。"""
    app, bridge = FakeApp(), FakeBridge()
    runtime = UnifiedMCPApp(app, [definition("demo")], lambda spec: bridge)
    with pytest.raises(RuntimeError):
        runtime.chat(NS(name="mcp__demo__add", arguments={"a": 1, "b": 2}))
    runtime.start()
    runtime.close()
    runtime.close()
    assert app.closed == bridge.close_count == 1
    with pytest.raises(RuntimeError):
        runtime.content("demo")


def test_unapproved_resource_has_no_server_side_effects():
    """内容白名单为空时，宿主不得访问任何外部 Resource 或 Prompt。"""
    bridge = FakeBridge()
    with UnifiedMCPApp(FakeApp(), [definition("demo")], lambda spec: bridge) as runtime:
        content = runtime.content("demo")
        with pytest.raises(PermissionError):
            content.read_resource("devpilot://docs/guide")
        with pytest.raises(PermissionError):
            content.get_prompt("review", {"file": "main.py"})
        assert bridge.calls == []


def test_trusted_json_configuration_retains_tool_allowlist_and_no_secret_in_status(tmp_path):
    """可信 JSON Server 配置加载后只暴露白名单内的工具且状态信息不包含环境密钥。"""
    conf = tmp_path / "servers.json"
    conf.write_text(json.dumps({"servers": [{"alias": "docs", "command": "python", "args": ["server.py"], "env_from": ["PRIVATE_KEY"], "allow_tools": ["echo"]}]}), encoding="utf-8")
    configs = load_server_definitions(conf, environment={"PRIVATE_KEY": "SECRET-DO-NOT-LOG"})
    assert configs[0].local.env["PRIVATE_KEY"] == "SECRET-DO-NOT-LOG"
    app = FakeApp(approve=lambda name, args: True)
    with UnifiedMCPApp(app, configs, lambda spec: FakeBridge()) as runtime:
        assert tuple(runtime.statuses()["docs"].tools) == ("mcp__docs__echo",)
        assert "SECRET-DO-NOT-LOG" not in repr(runtime.statuses())


def test_host_close_reclaims_v5_when_mcp_close_raises():
    """MCP 连接清理报错时 V5.7 App 仍然必须执行 shutdown。"""
    class BrokenBridge(FakeBridge):
        """模拟关闭连接时发生异常。"""

        def close(self):
            """在连接清理时抛错，检查剩余 Shutdown 是否仍能执行。"""
            raise OSError("cleanup failure")

    app = FakeApp()
    runtime = UnifiedMCPApp(app, [definition("bad")], lambda spec: BrokenBridge())
    runtime.start()
    with pytest.raises(ExceptionGroup):
        runtime.close()
    assert app.closed == 1
