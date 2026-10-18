"""V6.5：同时提供 Resources、Prompts 和普通工具的 HTTP MCP 教学 Server。"""

import argparse

from mcp.server import MCPServer

mcp = MCPServer("DevPilot V6.5 Content Server")


@mcp.resource("devpilot://docs/guide", mime_type="text/markdown")
def guide() -> str:
    """按需返回 DevPilot 项目开发规范，而非直接执行任何操作。"""
    return "# DevPilot guide\nUse pytest to verify a change before reporting completion."


@mcp.resource("devpilot://docs/{topic}", mime_type="text/plain")
def topic_doc(topic: str) -> str:
    """根据 URI 参数按需返回指定主题的教学内容。"""
    return f"DevPilot topic: {topic}"


@mcp.prompt()
def review_request(filename: str) -> str:
    """为用户选择的文件生成一个待审核的代码审查提示模板。"""
    return f"Review {filename}; do not modify files before approval."


@mcp.tool()
def ping() -> str:
    """用于确认这台 MCP Server 的工具仍可正常调用。"""
    return "pong"


def main() -> None:
    """按命令行端口在本机启动真正的 Streamable HTTP MCP Server。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
