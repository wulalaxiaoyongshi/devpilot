"""V6.4：确定性验证多 Server 注册、故障隔离、重连与配置安全。"""

import json
import threading
import time
from pathlib import Path

import pytest

from devpilot.mcp.adapter import v1_tool_name
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.multi_server import MCPConnectionErrors, MCPMultiServerManager, MCPToolSchemaChanged, ServerState
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.mcp.results import MCPToolInfo, MCPToolResult
from devpilot.mcp.server_config import MCPServerDefinition, load_server_definitions


ADD = MCPToolInfo("add", "Add two numbers", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"], "additionalProperties": False})
ECHO = MCPToolInfo("echo", "Echo a string", {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]})


class FakeRegistry:
    """使用最小 V1 Registry 契约检测动态工具注册和重复名称。"""

    def __init__(self):
        """初始化测试专用的工具字典。"""
        self.tools = {}

    def get(self, name):
        """根据工具名称读取已经注册的 V1 Tool。"""
        return self.tools.get(name)

    def register(self, tool):
        """拒绝覆盖同名工具并记录新工具。"""
        if tool.name in self.tools:
            raise ValueError("duplicate tool")
        self.tools[tool.name] = tool


class FakeBridge:
    """模拟 MCPBridge 生命周期、工具发现及单 Server 故障。"""

    def __init__(self, tools=None, fail_start=False, error=None, started=None, release=None):
        """配置本测试需要的发现结果、启动故障或工具执行阻塞。"""
        self.tools = list(tools if tools is not None else [ADD])
        self.fail_start = fail_start
        self.error = error
        self.started = started
        self.release = release
        self.calls = []
        self.starts = 0
        self.closes = 0

    def start(self):
        """模拟启动 Server 并按配置决定是否失败。"""
        self.starts += 1
        if self.fail_start:
            raise ConnectionError("server offline")
        return self

    def list_tools(self):
        """向管理器返回当前 Server 的发现快照。"""
        return list(self.tools)

    def call_tool(self, name, arguments):
        """记录真正被转发的 ToolCall，并可模拟慢调用或连接错误。"""
        self.calls.append((name, arguments))
        if self.started is not None:
            self.started.set()
        if self.release is not None and not self.release.wait(3):
            raise TimeoutError("test timed out")
        if self.error is not None:
            raise self.error
        return MCPToolResult(name, False, (f"{name}:ok",), {"ok": True}, ())

    def close(self):
        """记录连接关闭次数，以便断言没有资源泄漏。"""
        self.closes += 1


def defn(alias, allow=None):
    """快速构造仅用于测试的 Server 配置。"""
    return MCPServerDefinition(alias, LocalServerConfig("python", ("server.py",)), allow_tools=allow)


def test_two_servers_discover_and_register_namespaced_tools():
    """两台 Server 可以暴露同名工具，但 V1 中的命名空间保证不会冲突。"""
    registry = FakeRegistry()
    bridges = {"alpha": FakeBridge([ADD, ECHO]), "beta": FakeBridge([ADD])}
    with MCPMultiServerManager(registry, [defn("alpha"), defn("beta")], bridge_factory=lambda spec: bridges[spec.alias]) as manager:
        statuses = manager.connect_all()
        assert all(s.state == ServerState.CONNECTED for s in statuses.values())
        assert set(registry.tools) == {"mcp__alpha__add", "mcp__alpha__echo", "mcp__beta__add"}
        assert "MCP RESULT" in registry.get("mcp__alpha__add").handler({"a": 1, "b": 2})
        assert bridges["alpha"].calls == [("add", {"a": 1, "b": 2})]
        assert bridges["beta"].calls == []
    assert bridges["alpha"].closes == bridges["beta"].closes == 1


def test_one_server_fails_without_blocking_others():
    """连接失败只让故障 Server 降级，其他 Server 的工具仍然可用。"""
    registry = FakeRegistry()
    bridges = {"broken": FakeBridge(fail_start=True), "healthy": FakeBridge([ECHO])}
    with MCPMultiServerManager(registry, [defn("broken"), defn("healthy")], lambda spec: bridges[spec.alias]) as manager:
        assert manager.connect_all()["broken"].state == ServerState.DEGRADED
        assert manager.status("healthy").state == ServerState.CONNECTED
        assert "MCP RESULT" in registry.get("mcp__healthy__echo").handler({"message": "hello"})
        assert manager.status("broken").last_error.startswith("ConnectionError")


def test_strict_connect_reports_all_failures_after_trying_every_server():
    """严格模式会在逐台尝试后报告所有故障，而不是遇到第一台失败就停止。"""
    registry = FakeRegistry()
    with MCPMultiServerManager(registry, [defn("bad"), defn("good")], lambda spec: FakeBridge(fail_start=spec.alias == "bad")) as manager:
        with pytest.raises(MCPConnectionErrors) as error:
            manager.connect_all(strict=True)
        assert set(error.value.failures) == {"bad"}
        assert manager.status("good").state == ServerState.CONNECTED


def test_reconnect_uses_same_v1_handler_and_does_not_duplicate_registry():
    """重连会替换长连接而不重新注册工具，旧 V1 Handler 自动使用新 Server。"""
    old, new = FakeBridge([ADD]), FakeBridge([ADD])
    registry = FakeRegistry()
    bridges = iter([old, new])
    with MCPMultiServerManager(registry, [defn("one")], lambda spec: next(bridges)) as manager:
        manager.connect("one")
        handler = registry.get("mcp__one__add").handler
        assert "MCP RESULT" in handler({"a": 1, "b": 2})
        manager.reconnect("one")
        assert manager.status("one").generation == 2
        assert old.closes == 1
        assert registry.get("mcp__one__add").handler is handler
        assert "MCP RESULT" in handler({"a": 7, "b": 8})
        assert old.calls == [("add", {"a": 1, "b": 2})]
        assert new.calls == [("add", {"a": 7, "b": 8})]
        assert len(registry.tools) == 1


def test_reconnect_schema_drift_fails_closed_without_tool_replacement():
    """Server 重连后输入 Schema 变化会被拒绝，旧 Handler 不会悄悄执行新接口。"""
    old = FakeBridge([ADD])
    drift = MCPToolInfo("add", "Changed", {"type": "object", "properties": {"x": {"type": "string"}}})
    new = FakeBridge([drift])
    registry = FakeRegistry()
    bridges = iter([old, new])
    with MCPMultiServerManager(registry, [defn("one")], lambda spec: next(bridges)) as manager:
        manager.connect("one")
        with pytest.raises(MCPToolSchemaChanged):
            manager.reconnect("one")
        assert manager.status("one").state == ServerState.DEGRADED
        assert new.closes == old.closes == 1
        assert "MCP_TRANSPORT_ERROR" in registry.get("mcp__one__add").handler({"a": 1, "b": 2})
        assert old.calls == new.calls == []


def test_transport_failure_marks_only_its_own_server_degraded():
    """运行期间断线只影响对应 Server，不能让另一台健康服务一起失败。"""
    bad = FakeBridge(error=ConnectionError("gone"))
    good = FakeBridge()
    registry = FakeRegistry()
    bridges = {"bad": bad, "good": good}
    with MCPMultiServerManager(registry, [defn("bad"), defn("good")], lambda spec: bridges[spec.alias]) as manager:
        manager.connect_all()
        assert "MCP_TRANSPORT_ERROR" in registry.get("mcp__bad__add").handler({"a": 1, "b": 2})
        assert manager.status("bad").state == ServerState.DEGRADED
        assert manager.status("good").state == ServerState.CONNECTED
        assert "MCP RESULT" in registry.get("mcp__good__add").handler({"a": 3, "b": 4})


def test_disconnect_keeps_names_and_explicit_connect_reuses_tool_handler():
    """停止服务后 Tool 仍存在但不可调用，重新建连后继续使用原 Tool。"""
    first, second = FakeBridge(), FakeBridge()
    bridges = iter([first, second])
    registry = FakeRegistry()
    with MCPMultiServerManager(registry, [defn("one")], lambda spec: next(bridges)) as manager:
        manager.connect("one")
        handler = registry.get("mcp__one__add").handler
        assert manager.disconnect("one").state == ServerState.STOPPED
        assert "MCP_TRANSPORT_ERROR" in handler({"a": 1, "b": 2})
        assert manager.status("one").state == ServerState.STOPPED
        manager.connect("one")
        assert "MCP RESULT" in handler({"a": 1, "b": 2})
        assert first.closes == 1
        assert len(registry.tools) == 1


def test_allowlist_limits_registration_and_empty_list_exposes_nothing():
    """服务端暴露额外工具时，只有宿主明确允许的工具才会注册给模型。"""
    registry = FakeRegistry()
    with MCPMultiServerManager(registry, [defn("partial", frozenset({"add"})), defn("empty", frozenset())], lambda spec: FakeBridge([ADD, ECHO])) as manager:
        manager.connect_all(strict=True)
        assert set(registry.tools) == {"mcp__partial__add"}
        assert manager.status("empty").tools == ()


def test_collision_preserves_existing_registry_tool_and_closes_candidate():
    """注册发现与已有 V1 Tool 重名时，不覆盖旧工具且清理新连接。"""
    registry = FakeRegistry()
    sentinel = object()
    registry.tools["mcp__one__add"] = sentinel
    bridge = FakeBridge()
    with MCPMultiServerManager(registry, [defn("one")], lambda spec: bridge) as manager:
        assert manager.connect_all()["one"].state == ServerState.DEGRADED
        assert bridge.closes == 1
        assert registry.get("mcp__one__add") is sentinel


def test_schema_healthcheck_detects_mutation_and_disables_server():
    """健康检查发现服务器工具定义被替换时会断开连接并拒绝原 Handler 执行。"""
    registry, bridge = FakeRegistry(), FakeBridge()
    with MCPMultiServerManager(registry, [defn("one")], lambda spec: bridge) as manager:
        manager.connect("one")
        bridge.tools = [MCPToolInfo("add", "new", {"type": "object", "properties": {"y": {"type": "string"}}})]
        with pytest.raises(MCPToolSchemaChanged):
            manager.check_health("one")
        assert manager.status("one").state == ServerState.DEGRADED
        assert bridge.closes == 1
        assert "MCP_TRANSPORT_ERROR" in registry.get("mcp__one__add").handler({"a": 2, "b": 3})


def test_servers_have_separate_execution_paths():
    """一台 Server 的慢调用不会挡住另一台 Server 的独立连接。"""
    started, release = threading.Event(), threading.Event()
    slow, fast = FakeBridge(started=started, release=release), FakeBridge()
    registry = FakeRegistry()
    bridges = {"slow": slow, "fast": fast}
    with MCPMultiServerManager(registry, [defn("slow"), defn("fast")], lambda spec: bridges[spec.alias]) as manager:
        manager.connect_all(strict=True)
        result = []
        thread = threading.Thread(target=lambda: result.append(registry.get("mcp__slow__add").handler({"a": 1, "b": 2})))
        thread.start()
        assert started.wait(2)
        begin = time.monotonic()
        try:
            assert "MCP RESULT" in registry.get("mcp__fast__add").handler({"a": 3, "b": 4})
            assert time.monotonic() - begin < 1.0
        finally:
            release.set()
            thread.join(2)
        assert len(result) == 1


def test_configuration_loads_allowlist_and_env_from_without_embedded_secret(tmp_path):
    """从 JSON 读取 Server 时只转发获准环境变量，不把凭据写入状态快照。"""
    file = tmp_path / "mcp_servers.json"
    file.write_text(json.dumps({"servers": [{"alias": "issues", "command": "python", "args": ["server.py"], "env_from": ["ACCESS_TOKEN"], "allow_tools": ["add"], "timeout_seconds": 5}]}), encoding="utf-8")
    specs = load_server_definitions(file, environment={"PATH": "bin", "ACCESS_TOKEN": "secret123", "UNRELATED": "hidden"})
    assert specs[0].local.env == {"PATH": "bin", "ACCESS_TOKEN": "secret123"}
    assert specs[0].allow_tools == frozenset({"add"})
    with MCPMultiServerManager(FakeRegistry(), specs, lambda spec: FakeBridge()) as manager:
        assert "secret123" not in repr(manager.status("issues"))


@pytest.mark.parametrize("payload", [
    {"servers": [{"alias": "a", "command": "x"}, {"alias": "a", "command": "y"}]},
    {"servers": [{"alias": "Bad!", "command": "x"}]},
    {"servers": [{"alias": "one", "command": "x", "unknown": "y"}]},
    {"servers": [{"alias": "one", "command": "x", "env_from": ["PASSWORD"]}]},
    {"servers": [{"alias": "one", "command": "x", "allow_tools": "add"}]},
])
def test_invalid_configuration_fails_fast(tmp_path, payload):
    """配置包含重复别名、非法名称、未知字段或缺失环境变量时立即拒绝。"""
    file = tmp_path / "bad.json"
    file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        load_server_definitions(file, environment={})


def test_permission_policy_remains_compatible_with_v1():
    """多 Server 仍然复用 V6.2 的 MCP 权限策略，新别名不会绕过宿主审批。"""
    class Base:
        def check(self, call):
            return None

    class Call:
        name = "mcp__issues__add"
        arguments = {"a": 1, "b": 2}

    assert MCPPermissionPolicy(Base()).check(Call()) is not None
    assert MCPPermissionPolicy(Base(), lambda name, args: name == "mcp__issues__add").check(Call()) is None


def test_real_two_stdio_servers_when_mcp_sdk_installed():
    """本机装有官方 SDK 时通过两套独立子进程实际验证多 Server 调用和关闭。"""
    pytest.importorskip("mcp")
    config = LocalServerConfig.demo()
    specs = [MCPServerDefinition("first", config, frozenset({"add"}), timeout_seconds=10), MCPServerDefinition("second", config, frozenset({"echo"}), timeout_seconds=10)]
    registry = FakeRegistry()
    with MCPMultiServerManager(registry, specs) as manager:
        manager.connect_all(strict=True)
        assert "7" in registry.get("mcp__first__add").handler({"a": 3, "b": 4})
        assert "hello" in registry.get("mcp__second__echo").handler({"message": "hello"})
