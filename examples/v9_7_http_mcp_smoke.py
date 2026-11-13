"""直接通过 HTTPS 连接真实 MCP Server 并动态列出工具，不依赖 Docker 或 DeepSeek。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from devpilot.mcp.content_client import HTTPMCPClient
from devpilot.mcp.server_config import load_server_definitions


async def discover(config: Path, alias: str) -> dict:
    """经 DevPilot V6 HTTP Client 完成真实 MCP 握手并获取远端工具名称。"""
    definitions = load_server_definitions(config)
    selected = [definition for definition in definitions if definition.alias == alias]
    if len(selected) != 1 or selected[0].remote is None:
        raise ValueError(f"未找到 HTTP MCP Server: {alias}")
    async with HTTPMCPClient(selected[0].remote) as client:
        tools = await client.list_tools()
        return {"connected": True, "alias": alias, "server": client.connection_info(),
                "tools_count": len(tools), "tools": [tool.name for tool in tools]}


def main(argv: list[str] | None = None) -> int:
    """从 CLI 读取服务器配置并打印不包含认证信息的工具发现结果。"""
    parser = argparse.ArgumentParser(description="DevPilot 远程 HTTP MCP 真实连接测试")
    parser.add_argument("--config", type=Path, default=Path("examples/github_mcp_http_readonly.json"))
    parser.add_argument("--alias", default="github")
    args = parser.parse_args(argv)
    result = asyncio.run(discover(args.config, args.alias))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
