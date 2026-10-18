"""封装官方 MCP v2 Client，面向 DevPilot 提供本地 stdio 通信接口。"""  # 本节的核心代码

from contextlib import AsyncExitStack  # 安全管理异步连接的关闭生命周期
from typing import Any  # 接受 JSON 格式的 MCP 工具参数

from .config import LocalServerConfig  # 导入宿主程序提供的启动配置
from .results import MCPToolInfo, MCPToolResult, normalize_tool_result  # 导入项目自己的数据类型


class LocalMCPClient:  # 负责连接一个本地 MCP Server
    def __init__(self, config: LocalServerConfig) -> None:  # 接收可信宿主提供的 Server 启动配置
        self.config = config  # 保存启动命令和参数
        self._stack: AsyncExitStack | None = None  # 保存异步退出栈
        self._client: Any | None = None  # 连接后才持有官方 SDK Client

    def _new_sdk_client(self) -> Any:  # 根据配置创建一个新的官方 SDK Client
        from mcp import Client, StdioServerParameters  # 仅在实际连接时要求安装 MCP v2
        params = StdioServerParameters(command=self.config.command, args=list(self.config.args), env=self.config.env)  # 指定 stdio Server 子进程
        return Client(params)  # 官方 SDK 将负责启动子进程、协商协议和通信

    async def __aenter__(self) -> "LocalMCPClient":  # 进入 async with 时真正建立连接
        if self._stack is not None:  # 防止重复进入同一个尚未关闭的连接
            raise RuntimeError("MCP client is already connected")  # 告诉调用者连接已经存在
        stack = AsyncExitStack()  # 为本次连接创建新的资源清理栈
        try:  # 尝试启动并连接 Server
            sdk_client = await stack.enter_async_context(self._new_sdk_client())  # 启动子进程并完成 SDK 连接
        except BaseException:  # 连接失败或被取消时也应清理已打开的资源
            await stack.aclose()  # 确保不会留下退出栈资源
            raise  # 将真实连接失败原因交给上层
        self._stack = stack  # 保存连接的清理栈
        self._client = sdk_client  # 保存已连接的 SDK Client
        return self  # 将本地客户端交给 async with 的调用者

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:  # 离开 async with 时关闭连接和 Server 进程
        stack = self._stack  # 获取本次连接的退出栈
        if stack is None:  # 已经关闭就无需重复操作
            return False  # 不吞掉外部异常
        self._stack = None  # 先清理连接标记，允许后面重新建立新连接
        self._client = None  # 不再暴露已经过期的 SDK Client
        return await stack.__aexit__(exc_type, exc, tb)  # 通知 SDK 正常关闭子进程和管道

    def _require_client(self) -> Any:  # 获取当前活跃 SDK Client
        if self._client is None:  # 如果还没进入 async with 或已关闭
            raise RuntimeError("Use 'async with LocalMCPClient(...)' before calling MCP methods")  # 防止误用已关闭连接
        return self._client  # 返回当前连接对象

    def connection_info(self) -> dict[str, str | None]:  # 获取连接和协议协商信息
        client = self._require_client()  # 获取已经连接的 SDK Client
        server_info = client.server_info  # SDK 提供的 Server 身份信息
        return {"server_name": server_info.name if server_info is not None else None, "protocol_version": str(client.protocol_version)}  # 返回方便日志和调试的字典

    async def list_tools(self) -> list[MCPToolInfo]:  # 发现 Server 对外暴露的工具
        client = self._require_client()  # 确保已建立 MCP 连接
        response = await client.list_tools()  # 发送 tools/list 并等待返回
        return [MCPToolInfo(name=tool.name, description=tool.description or "", input_schema=tool.input_schema) for tool in response.tools]  # 将 SDK 结果转换为项目自己的工具定义

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:  # 调用 Server 的指定工具
        client = self._require_client()  # 获取当前活跃连接
        if not name.strip():  # 工具名称不能为空
            raise ValueError("Tool name cannot be empty")  # 提前拒绝无效调用
        raw = await client.call_tool(name, arguments or {})  # 发送 tools/call 并等待工具返回
        return normalize_tool_result(name, raw)  # 转换为统一结果，保留 is_error 标志
