"""V6.5：让 V6.1 的 stdio Client 和新 HTTP Client 读取 MCP Resources 与 Prompts。"""

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from devpilot.mcp.client import LocalMCPClient
from devpilot.mcp.config import LocalServerConfig


@dataclass(frozen=True)
class RemoteServerConfig:
    """保存宿主明确批准的 HTTP MCP 地址，并阻止远程明文通信。"""
    url: str
    def __post_init__(self) -> None:
        """校验远程 Server 地址必须是 HTTPS 或本机回环 HTTP。"""
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Invalid MCP endpoint URL")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("Invalid MCP endpoint port") from error
        if parsed.scheme == "http":
            hostname = parsed.hostname.lower()
            try:
                loopback = ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname == "localhost"
            if not loopback:
                raise ValueError("Insecure remote MCP URL: HTTPS required outside loopback")
        if parsed.path not in {"/mcp", "/mcp/"}:
            raise ValueError("MCP endpoint must use the explicit /mcp path")
        if parsed.query:
            raise ValueError("MCP endpoint must not contain query parameters or secrets")


class ContentMCPClient(LocalMCPClient):
    """继承 V6.1 的连接生命周期，并补上 Resources 与 Prompts 协议动词。"""

    def __init__(self, config: LocalServerConfig):
        """将现有本地 Server 配置交给原 V6.1 Client。"""
        super().__init__(config)

    async def list_resources(self, cursor: str | None = None):
        """列出指定页的固定 URI 资源并保留 SDK 分页游标。"""
        return await self._require_client().list_resources(cursor=cursor)

    async def list_resource_templates(self, cursor: str | None = None):
        """列出指定页的带参数 URI 资源模板。"""
        return await self._require_client().list_resource_templates(cursor=cursor)

    async def read_resource(self, uri: str):
        """从指定 URI 读取外部资源内容而不直接注入模型。"""
        return await self._require_client().read_resource(uri)

    async def list_prompts(self, cursor: str | None = None):
        """列出指定页的可用 Prompt 模板与参数要求。"""
        return await self._require_client().list_prompts(cursor=cursor)

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None):
        """按宿主选择的模板名和字符串参数获取渲染好的 Prompt 消息。"""
        return await self._require_client().get_prompt(name, arguments or {})


class HTTPMCPClient(ContentMCPClient):
    """沿用 V6.1 的自动关闭逻辑，通过 Streamable HTTP 访问受信任的远程 Server。"""

    def __init__(self, config: RemoteServerConfig):
        """保存经过校验的 HTTP 配置，连接只在 async with 中建立。"""
        self.remote = config
        super().__init__(LocalServerConfig(command="__http_not_a_subprocess__"))

    def _new_sdk_client(self):
        """让 MCP v2 SDK 根据 URL 自动选择 Streamable HTTP 传输。"""
        from mcp import Client
        return Client(self.remote.url)
