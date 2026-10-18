"""演示连接本地 Server、发现工具、调用工具和关闭连接。"""  # 不需要模型 API Key

import asyncio  # 驱动异步 MCP 客户端

from devpilot.mcp.client import LocalMCPClient, LocalServerConfig  # 使用 DevPilot 自己定义的 API


async def main() -> None:  # 定义演示程序的异步入口
    config = LocalServerConfig.demo()  # 构建跨平台的 Python 子进程启动命令
    async with LocalMCPClient(config) as client:  # 建立连接，并在退出时自动关闭子进程
        print("连接信息：", client.connection_info())  # 展示 Server 名称和协议版本
        tools = await client.list_tools()  # 向 Server 获取可用工具列表
        for tool in tools:  # 逐个查看 Server 声明的工具
            print(f"发现工具：{tool.name}，说明：{tool.description}")  # 显示工具名称与描述
        result = await client.call_tool("add", {"a": 3, "b": 4})  # 调用远程 Server 内的加法函数
        print("add 的文本结果：", result.text)  # 打印人类可读的返回内容
        print("add 的结构化结果：", result.structured_content)  # 打印程序可读取的结果
        echo = await client.call_tool("echo", {"message": "DevPilot"})  # 执行回显工具
        print("echo 结果：", echo.text)  # 显示 Server 返回的字符串
        failed = await client.call_tool("fail", {"message": "演示业务失败"})  # 故意触发业务错误
        print("fail 的 is_error：", failed.is_error)  # 验证错误表现为结构化工具结果
        print("fail 的错误消息：", failed.text)  # 输出 Server 返回的错误内容
    print("连接已经关闭")  # 离开 async with 后才打印连接关闭消息


if __name__ == "__main__":  # 直接运行示例模块时进入这里
    asyncio.run(main())  # 启动 asyncio 事件循环直到 Demo 执行完毕
