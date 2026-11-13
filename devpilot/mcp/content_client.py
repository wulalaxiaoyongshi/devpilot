"""V6.5：让 V6.1 的 stdio Client 和新 HTTP Client 读取 MCP Resources 与 Prompts。"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
import re
from types import MappingProxyType
from typing import Mapping
from ipaddress import ip_address
from urllib.parse import urlsplit

from devpilot.mcp.client import LocalMCPClient
from devpilot.mcp.config import LocalServerConfig


@dataclass(frozen=True)
class RemoteServerConfig:
    """保存宿主明确批准的 HTTP MCP 地址，并阻止远程明文通信。"""
    url: str
    auth_token_env: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

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
        if self.auth_token_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.auth_token_env):
            raise ValueError("auth_token_env must name one environment variable")
        if not isinstance(self.headers, Mapping):
            raise ValueError("MCP HTTP headers must be a mapping")
        validated: dict[str, str] = {}
        for key, value in self.headers.items():
            if not isinstance(key, str) or not re.fullmatch(r"X-MCP-[A-Za-z0-9-]+", key, flags=re.IGNORECASE):
                raise ValueError("Only non-secret X-MCP-* headers are supported in configuration")
            if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("MCP header values must be printable strings")
            if key.lower() in {existing.lower() for existing in validated}:
                raise ValueError("Duplicate MCP HTTP header")
            validated[key] = value
        object.__setattr__(self, "headers", MappingProxyType(validated))


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
        """连接 HTTP MCP 时从环境变量注入 Bearer Token，并管理 SDK 与 HTTP 客户端的生命周期。"""
        from mcp import Client
        if self.remote.auth_token_env is None and not self.remote.headers:
            return Client(self.remote.url)
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        @asynccontextmanager
        async def authenticated_client():
            """确保 Streamable HTTP 使用的认证客户端会随 MCP 会话一起关闭。"""
            headers = dict(self.remote.headers)
            if self.remote.auth_token_env:
                token = os.environ.get(self.remote.auth_token_env, "").strip()
                if not token:
                    raise RuntimeError(f"Missing MCP HTTP credential environment variable: {self.remote.auth_token_env}")
                headers["Authorization"] = f"Bearer {token}"
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(30.0, read=300.0),
            ) as http_client:
                transport = streamable_http_client(self.remote.url, http_client=http_client)
                async with Client(transport) as client:
                    yield client

        return authenticated_client()
