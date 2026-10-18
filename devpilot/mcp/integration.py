"""V6.2：统一管理 MCP Bridge 的连接、工具注册和关闭生命周期。"""

from contextlib import ExitStack
from pathlib import Path
from typing import Any

from devpilot.mcp.adapter import MCPToolAdapter
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.sync_bridge import MCPBridge


class MCPToolIntegration:
    """宿主通过这个入口连接一个或多个本地 Server，并把它们的工具注册到 V1。"""

    def __init__(self, registry: Any, timeout: float = 30.0):
        """宿主创建扩展层时调用，绑定 V1 Registry 并设置 MCP 调用超时。"""
        self.registry = registry
        self.timeout = timeout
        self._stack = ExitStack()
        self._aliases: set[str] = set()
        self._closed = False

    def add_local(self, alias: str, config: LocalServerConfig, allow_tools: set[str] | None = None) -> list[str]:
        """宿主配置本地 MCP Server 时调用，启动连接并将发现的工具挂进 V1 Registry。"""
        if self._closed:
            raise RuntimeError("MCPToolIntegration is closed")
        if alias in self._aliases:
            raise ValueError(f"MCP alias already connected: {alias}")
        bridge = MCPBridge.from_local(config, timeout=self.timeout)
        bridge.start()
        try:
            adapter = MCPToolAdapter(bridge, alias)
            names = adapter.register(self.registry, allow_tools=allow_tools)
        except BaseException:
            bridge.close()
            raise
        self._stack.callback(bridge.close)
        self._aliases.add(alias)
        return names

    def close(self) -> None:
        """DevPilot 退出时调用，按连接顺序的逆序关闭所有 MCP 子进程。"""
        if self._closed:
            return
        self._closed = True
        self._stack.close()

    def __enter__(self) -> "MCPToolIntegration":
        """宿主进入 with 语句时调用，以便统一管理多个 MCP Server 的生命周期。"""
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """宿主离开 with 语句时调用，关闭所有仍然活跃的 Server 连接。"""
        self.close()
