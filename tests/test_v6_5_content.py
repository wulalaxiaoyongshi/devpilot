"""V6.5：使用 Fake SDK 验证分页、资源过滤、Prompt、HTTP 配置和桥接线程生命周期。"""

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from devpilot.mcp.content_client import ContentMCPClient, HTTPMCPClient, RemoteServerConfig
from devpilot.mcp.content_runtime import MCPContentRuntime
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.sync_bridge import MCPBridge
from devpilot.mcp.server_config import MCPServerDefinition, load_server_definitions


class FakeContentSDK:
    """模拟官方 Client 的 Resources/Prompts 协议返回而不依赖网络。"""

    def __init__(self):
        """保存调用记录和调用线程以验证桥接器没有新建事件循环。"""
        self.calls = []
        self.threads = set()

    async def list_resources(self, cursor=None):
        """返回两页资源，用于验证完整分页和白名单。"""
        self._record("resources", cursor)
        if cursor is None:
            return NS(resources=[NS(name="guide", uri="devpilot://docs/guide", mime_type="text/markdown", description="Guide")], next_cursor="next")
        return NS(resources=[NS(name="secret", uri="secret://token", mime_type="text/plain", description="Secret")], next_cursor=None)

    async def list_resource_templates(self, cursor=None):
        """返回一个未展开的 URI 模板。"""
        self._record("templates", cursor)
        return NS(resource_templates=[NS(name="topic", uri_template="devpilot://docs/{topic}", mime_type="text/plain")], next_cursor=None)

    async def read_resource(self, uri):
        """根据 URI 返回文本和 Blob 混合内容。"""
        self._record("read", uri)
        return NS(contents=[NS(text="a" * 20, mime_type="text/plain"), NS(blob="AQID", mime_type="image/png")])

    async def list_prompts(self, cursor=None):
        """返回允许的和未允许的两个 Prompt。"""
        self._record("prompts", cursor)
        return NS(prompts=[NS(name="review_request", description="Review code", arguments=[NS(name="filename", required=True)]), NS(name="unsafe", arguments=[])], next_cursor=None)

    async def get_prompt(self, name, arguments):
        """生成一条文本消息和一条未处理图片消息。"""
        self._record("get_prompt", name, arguments)
        return NS(messages=[NS(role="user", content=NS(type="text", text="Review " + arguments["filename"])), NS(role="user", content=NS(type="image", data="base64"))])

    async def list_tools(self):
        """模拟原 V6.2 工具发现以验证向后兼容。"""
        self._record("list_tools")
        return [NS(name="ping", input_schema={"type": "object"})]

    async def call_tool(self, name, arguments):
        """模拟旧 MCP Tool 路径仍能正常执行。"""
        self._record("call_tool", name, arguments)
        return NS(name=name, text="pong")

    def _record(self, *args):
        """记录实际执行路径和线程名称。"""
        self.calls.append(args)
        self.threads.add(threading.current_thread().name)


class FakeAsyncContentClient:
    """将 Fake SDK 包装成原 V6.3 Bridge 可接受的异步上下文管理器。"""

    def __init__(self, sdk):
        """保存模拟协议客户端的引用。"""
        self.sdk = sdk

    async def __aenter__(self):
        """返回模拟协议客户端供 Bridge 在单协程内使用。"""
        return self.sdk

    async def __aexit__(self, *args):
        """标记资源退出并保留外部异常。"""
        return False


def make_content(bridge):
    """创建具有最小权限和小尺寸预算的宿主内容访问层。"""
    return MCPContentRuntime(bridge, allowed_uri_prefixes=("devpilot://docs/",), allowed_prompts=frozenset({"review_request"}), max_chars=12)


def test_http_config_enforces_https_for_remote():
    """远程明文 HTTP、URL 密码与非 /mcp 路径均会拒绝。"""
    for url in ("http://example.org/mcp", "https://user:pass@example.org/mcp", "https://example.org/other", "file:///mcp", "https://example.org/mcp?token=x"):
        with pytest.raises(ValueError):
            RemoteServerConfig(url)
    assert RemoteServerConfig("http://127.0.0.1:8123/mcp").url.endswith("/mcp")
    assert RemoteServerConfig("https://example.org/mcp").url.startswith("https")


def test_live_bridge_serves_resources_prompts_and_legacy_tools():
    """真实线程桥接器既能执行新内容请求，也不破坏 V6.2 旧工具操作。"""
    sdk = FakeContentSDK()
    with MCPBridge(lambda: FakeAsyncContentClient(sdk)) as bridge:
        runtime = make_content(bridge)
        assert [r.uri for r in runtime.list_resources()] == ["devpilot://docs/guide"]
        assert [t.uri_template for t in runtime.list_resource_templates()] == ["devpilot://docs/{topic}"]
        assert [p.name for p in runtime.list_prompts()] == ["review_request"]
        assert bridge.call_tool("ping", {}).text == "pong"
        content = runtime.read_resource("devpilot://docs/guide")
        assert content.text == "a" * 12 and content.truncated
        assert content.binary_types == ("image/png",)
        prompt = runtime.get_prompt("review_request", {"filename": "main.py"})
        assert prompt.messages == (("user", "Review main."),)
        assert prompt.other_types == ("image",)
    assert len(sdk.threads) == 1
    assert "MainThread" not in sdk.threads
    assert ("resources", "next") in sdk.calls


def test_resource_and_prompt_allowlist_deny_unapproved_reads():
    """未批准的 URI 和 Prompt 会在任何 MCP 网络请求前被阻止。"""
    sdk = FakeContentSDK()
    with MCPBridge(lambda: FakeAsyncContentClient(sdk)) as bridge:
        content = make_content(bridge)
        with pytest.raises(PermissionError):
            content.read_resource("secret://token")
        with pytest.raises(PermissionError):
            content.get_prompt("unsafe", {})
        assert sdk.calls == []


def test_prompt_argument_types_must_be_strings():
    """Prompt 参数必须保持 MCP 要求的字符串到字符串映射。"""
    with MCPBridge(lambda: FakeAsyncContentClient(FakeContentSDK())) as bridge:
        with pytest.raises(ValueError):
            make_content(bridge).get_prompt("review_request", {"filename": 123})


def test_pagination_repeating_cursor_fails():
    """恶意服务器重复返回同一游标时会停止分页防止无限循环。"""
    class BadBridge:
        """模拟一直返回相同分页游标的故障服务器。"""
        def list_resources(self, cursor=None):
            """不断返回相同游标以触发限额保护。"""
            return NS(resources=[], next_cursor="same")
    with pytest.raises(ValueError, match="repeating cursor"):
        make_content(BadBridge()).list_resources()


def test_pagination_page_limit_fails():
    """Server 持续返回新游标时会触发最大分页数限制。"""
    class InfiniteBridge:
        """模拟永远不会结束的 MCP 分页目录。"""
        def list_prompts(self, cursor=None):
            """持续产生新游标用于测试分页上限。"""
            return NS(prompts=[], next_cursor=str(int(cursor or 0) + 1))
    with pytest.raises(ValueError, match="max_pages"):
        MCPContentRuntime(InfiniteBridge(), max_pages=3).list_prompts()


def test_mixed_server_configuration_accepts_http_and_stdio(tmp_path):
    """V6.4 JSON 配置可以同时加载本地和 HTTPS MCP Server。"""
    import json
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": [{"alias": "local", "command": "python", "args": ["server.py"]}, {"alias": "remote", "url": "https://example.org/mcp", "allow_tools": ["ping"]}]}), encoding="utf-8")
    configs = load_server_definitions(path, environment={})
    assert configs[0].local is not None and configs[0].remote is None
    assert configs[1].remote is not None and configs[1].local is None
    with pytest.raises(ValueError):
        MCPServerDefinition(alias="x", local=LocalServerConfig("python"), remote=RemoteServerConfig("https://example.org/mcp"))


def test_http_client_constructs_sdk_client_lazily(monkeypatch):
    """HTTPMCPClient 只在真正建立连接时才创建 MCP SDK Client。"""
    import sys
    from types import ModuleType
    generated = []
    fake = ModuleType("mcp")
    fake.Client = lambda url: generated.append(url) or NS()
    monkeypatch.setitem(sys.modules, "mcp", fake)
    client = HTTPMCPClient(RemoteServerConfig("https://example.org/mcp"))
    assert generated == []
    client._new_sdk_client()
    assert generated == ["https://example.org/mcp"]


def test_content_client_delegates_to_sdk(monkeypatch):
    """V6.1 的资源与 Prompt 请求会透传 SDK v2 的真实方法和分页参数。"""
    sdk = FakeContentSDK()
    client = ContentMCPClient(LocalServerConfig("python"))
    monkeypatch.setattr(client, "_require_client", lambda: sdk)
    async def run():
        """在同一事件循环中调用全部新增的异步协议接口。"""
        await client.list_resources("next")
        await client.list_resource_templates()
        await client.read_resource("devpilot://docs/guide")
        await client.list_prompts()
        await client.get_prompt("review_request", {"filename": "x.py"})
    asyncio.run(run())
    assert ("resources", "next") in sdk.calls
    assert ("get_prompt", "review_request", {"filename": "x.py"}) in sdk.calls


def test_manager_uses_remote_bridge_for_remote_definitions(monkeypatch):
    """V6.4 多服务管理器为 HTTP 配置选择 from_http，而不启动本地子进程。"""
    from devpilot.mcp.multi_server import MCPMultiServerManager

    class Registry:
        """构造仅用于观察多 Server 运行的工具存储。"""
        def __init__(self):
            """创建空的 V1 工具集合。"""
            self.tools = {}
        def get(self, name):
            """按名称读取之前注册的 Tool。"""
            return self.tools.get(name)
        def register(self, tool):
            """记录管理器注册的 Tool。"""
            self.tools[tool.name] = tool

    class Bridge:
        """模拟不依赖网络的空工具集 MCP 连接。"""
        def start(self):
            """让多 Server 管理器认为这台 Server 已连接。"""
            return self
        def list_tools(self):
            """返回本测试无需注册工具的空列表。"""
            return []
        def close(self):
            """模拟关闭动作以确保生命周期无异常。"""
            return None

    created = []
    def from_http(cls, config, timeout):
        """记录 HTTP Server 的配置用于断言传输选择。"""
        created.append((config.url, timeout))
        return Bridge()

    monkeypatch.setattr(MCPBridge, "from_http", classmethod(from_http))
    spec = MCPServerDefinition(alias="remote", remote=RemoteServerConfig("https://example.org/mcp"))
    with MCPMultiServerManager(Registry(), [spec]) as manager:
        assert manager.connect("remote").generation == 1
    assert created == [("https://example.org/mcp", 30.0)]


def test_real_streamable_http_server_with_mcp_sdk():
    """SDK 可用时启动真实 HTTP MCP Server，验证 Resources、Prompts 与旧 Tool 的完整链路。"""
    import socket
    import subprocess
    import sys
    import time

    pytest.importorskip("mcp")
    server_path = Path(__file__).resolve().parents[1] / "devpilot" / "mcp" / "servers" / "content_http_server.py"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(server_path), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge = None
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("MCP Server terminated during startup")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.1)
                try:
                    probe.connect(("127.0.0.1", port))
                except OSError:
                    time.sleep(0.1)
                    continue
                break
        bridge = MCPBridge.from_http(RemoteServerConfig(f"http://127.0.0.1:{port}/mcp"), timeout=15).start()
        runtime = MCPContentRuntime(bridge, allowed_uri_prefixes=("devpilot://docs/",), allowed_prompts=frozenset({"review_request"}))
        assert "devpilot://docs/guide" in [r.uri for r in runtime.list_resources()]
        assert "devpilot://docs/{topic}" in [r.uri_template for r in runtime.list_resource_templates()]
        assert "pytest" in runtime.read_resource("devpilot://docs/guide").text
        assert "Review main.py" in runtime.get_prompt("review_request", {"filename": "main.py"}).messages[0][1]
        assert "pong" in bridge.call_tool("ping", {}).text
    finally:
        if bridge is not None:
            bridge.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
