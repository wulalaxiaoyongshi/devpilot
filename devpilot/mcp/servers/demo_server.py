"""独立运行的本地 MCP Server；stdout 只用于 MCP 协议通信。"""  # 避免在服务启动期间随意 print

from mcp.server import MCPServer  # 导入官方 MCP v2 的 Server 类
from mcp.server.mcpserver.exceptions import ToolError  # 导入可转换为工具错误结果的异常

mcp = MCPServer("DevPilot Demo MCP Server")  # 创建可以通过 stdio 提供服务的 MCP Server


@mcp.tool()  # 将普通 Python 函数注册为 MCP 工具
def add(a: int, b: int) -> int:  # 接收两个整数并计算加法
    """计算两个整数的和。"""  # SDK 会把这句话作为工具描述
    return a + b  # 把计算结果返回给 MCP Client


@mcp.tool()  # 注册一个返回字符串的 MCP 工具
def echo(message: str) -> str:  # 接收调用方传入的字符串
    """原样回显用户传入的文本。"""  # 向客户端说明工具用途
    return f"echo: {message}"  # 返回 Server 处理后的文本


@mcp.tool()  # 注册用于演示业务错误的工具
def fail(message: str) -> str:  # 允许调用方指定错误信息
    """主动报告一个可预期的工具错误，用于测试错误返回。"""  # 说明该工具故意失败
    raise ToolError(message)  # 由 SDK 转成 is_error=True 的 MCP 工具结果


if __name__ == "__main__":  # 只有单独运行本文件时才启动 Server
    mcp.run()  # 使用默认 stdio 传输启动 Server 并等待客户端请求
