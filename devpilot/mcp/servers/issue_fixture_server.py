"""V9.4：使用真正 MCP stdio 协议提供一个只读、无外部凭据的 Issue 验收夹具。"""

from mcp.server import MCPServer

mcp = MCPServer("DevPilot V9.4 Issue Fixture")


@mcp.tool()
def get_issue(issue_number: int) -> str:
    """返回测试仓库中固定的登录错误码 Issue，非真实 GitHub 数据。"""
    if issue_number != 15:
        raise ValueError("Unknown fixture issue")
    return ("Issue #15: 登录失败状态码错误。密码错误时返回 500，必须改成 401；"
            "正确密码保持 200；需要实际 unittest 回归测试。")


if __name__ == "__main__":
    mcp.run()
