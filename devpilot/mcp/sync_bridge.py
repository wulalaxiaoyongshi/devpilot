"""V6.3：保留 V6.2 的单异步任务生命周期，并增加可判定的队列取消和超时状态。"""

import asyncio
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any, Callable

from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.results import MCPToolInfo, MCPToolResult


class MCPCallTimeout(TimeoutError):
    """告知调用者等待已超时，同时区分请求是否已经开始送往 Server。"""

    def __init__(self, operation: str, remote_may_have_executed: bool):
        """将远端副作用是否可能发生写入可检查的异常属性。"""
        self.remote_may_have_executed = remote_may_have_executed
        self.operation = operation
        note = "remote action may still finish" if remote_may_have_executed else "request was cancelled before dispatch"
        super().__init__(f"MCP {operation} timed out; {note}")


class MCPCallCancelled(RuntimeError):
    """表示宿主请求停止等待，执行中的远端操作未必实际取消。"""

    def __init__(self, remote_may_have_executed: bool):
        """标记本次取消是否发生在 Server 调用可能开始之后。"""
        self.remote_may_have_executed = remote_may_have_executed
        note = "remote action may still finish" if remote_may_have_executed else "request cancelled before dispatch"
        super().__init__("MCP caller cancelled; " + note)


@dataclass
class _Request:
    """封装一次待执行请求，并在请求开始与取消之间建立原子判定。"""

    operation: str
    arguments: tuple
    result: Future[Any] = field(default_factory=Future)
    _lock: Lock = field(default_factory=Lock)
    _started: bool = False
    _cancelled: bool = False

    def try_start(self) -> bool:
        """MCP 专用线程准备执行时检查是否已取消，未取消才标记为已开始。"""
        with self._lock:
            if self._cancelled:
                return False
            self._started = True
            return True

    def cancel_wait(self) -> bool:
        """等待线程超时或被取消时禁止尚未开始的请求，并返回是否可能已执行。"""
        with self._lock:
            self._cancelled = True
            return self._started


class MCPBridge:
    """为同步 V1 Handler 保持一个长期异步 MCP 连接，并管理请求超时和取消。"""

    def __init__(self, client_factory: Callable[[], AbstractAsyncContextManager], timeout: float = 30.0):
        """接受 MCP 客户端工厂并定义连接、执行和退出时的默认等待上限。"""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._factory = client_factory
        self._timeout = timeout
        self._lock = RLock()
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Request] | None = None
        self._ready: Future[None] = Future()
        self._closed = False

    @classmethod
    def from_local(cls, config: LocalServerConfig, timeout: float = 30.0) -> "MCPBridge":
        """宿主准备连接本地 stdio Server 时创建使用 V6.1 客户端的桥接器。"""
        from devpilot.mcp.content_client import ContentMCPClient
        return cls(lambda: ContentMCPClient(config), timeout=timeout)

    @classmethod
    def from_http(cls, config: "RemoteServerConfig", timeout: float = 30.0) -> "MCPBridge":
        """通过 V6.1 的异步生命周期桥接一台 Streamable HTTP Server。"""
        from devpilot.mcp.content_client import HTTPMCPClient
        return cls(lambda: HTTPMCPClient(config), timeout=timeout)

    async def _serve(self) -> None:
        """在专用事件循环的同一异步任务中完成 MCP 连接、队列处理和关闭。"""
        try:
            async with self._factory() as client:
                self._loop = asyncio.get_running_loop()
                self._queue = asyncio.Queue()
                self._ready.set_result(None)
                while True:
                    request = await self._queue.get()
                    if not request.try_start():
                        if not request.result.done():
                            request.result.set_exception(MCPCallCancelled(False))
                        continue
                    if request.operation == "stop":
                        request.result.set_result(None)
                        break
                    try:
                        if request.operation == "list":
                            value = await client.list_tools()
                        elif request.operation == "call":
                            value = await client.call_tool(*request.arguments)
                        elif request.operation == "resources":
                            value = await client.list_resources(cursor=request.arguments[0])
                        elif request.operation == "templates":
                            value = await client.list_resource_templates(cursor=request.arguments[0])
                        elif request.operation == "read_resource":
                            value = await client.read_resource(request.arguments[0])
                        elif request.operation == "prompts":
                            value = await client.list_prompts(cursor=request.arguments[0])
                        elif request.operation == "get_prompt":
                            value = await client.get_prompt(*request.arguments)
                        else:
                            raise ValueError(f"Unsupported MCP operation: {request.operation}")
                    except Exception as error:
                        request.result.set_exception(error)
                    else:
                        request.result.set_result(value)
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)
            raise
        finally:
            self._loop = None
            self._queue = None

    def _thread_main(self) -> None:
        """在独立线程启动 asyncio 事件循环，并确保断开的线程结束。"""
        try:
            asyncio.run(self._serve())
        except BaseException:
            pass

    def start(self) -> "MCPBridge":
        """初始化异步线程并等待 MCP Server 连接建立。"""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("MCPBridge can only be started once")
            self._thread = Thread(target=self._thread_main, name="devpilot-mcp", daemon=True)
            self._thread.start()
        try:
            self._ready.result(timeout=self._timeout)
        except FutureTimeout as error:
            raise MCPCallTimeout("connect", True) from error
        return self

    def _request(self, operation: str, arguments: tuple = (), timeout: float | None = None, cancel_event: Event | None = None) -> Any:
        """把同步请求送入异步队列，并在超时或取消时区分排队阶段与执行阶段。"""
        if current_thread() is self._thread:
            raise RuntimeError("MCPBridge cannot block its own event loop thread")
        duration = self._timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        with self._lock:
            if self._closed or self._thread is None or not self._thread.is_alive() or self._loop is None or self._queue is None:
                raise RuntimeError("MCPBridge is not connected")
            request = _Request(operation, arguments)
            self._loop.call_soon_threadsafe(self._queue.put_nowait, request)
        deadline = time.monotonic() + duration
        while True:
            if request.result.done():
                return request.result.result()
            if cancel_event is not None and cancel_event.is_set():
                started = request.cancel_wait()
                raise MCPCallCancelled(started)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                started = request.cancel_wait()
                raise MCPCallTimeout(operation, started)
            try:
                return request.result.result(timeout=min(remaining, 0.02 if cancel_event is not None else remaining))
            except FutureTimeout:
                continue

    def list_tools(self, timeout: float | None = None) -> list[MCPToolInfo]:
        """同步取得 MCP Server 动态公开的工具信息。"""
        return self._request("list", timeout=timeout)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None, cancel_event: Event | None = None) -> MCPToolResult:
        """同步调用 MCP 工具，并允许宿主设置等待超时或外部取消信号。"""
        return self._request("call", (name, arguments), timeout=timeout, cancel_event=cancel_event)

    def list_resources(self, cursor: str | None = None, timeout: float | None = None):
        """把同步资源目录查询投递到 MCP 的独立事件循环线程。"""
        return self._request("resources", (cursor,), timeout=timeout)

    def list_resource_templates(self, cursor: str | None = None, timeout: float | None = None):
        """在同一条长连接上读取一页资源模板。"""
        return self._request("templates", (cursor,), timeout=timeout)

    def read_resource(self, uri: str, timeout: float | None = None):
        """在 MCP 线程中读取指定 URI 的真实内容。"""
        return self._request("read_resource", (uri,), timeout=timeout)

    def list_prompts(self, cursor: str | None = None, timeout: float | None = None):
        """从同一条 MCP 连接查询一页 Prompt 模板。"""
        return self._request("prompts", (cursor,), timeout=timeout)

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None, timeout: float | None = None):
        """将宿主选择的 Prompt 请求转入 MCP 线程执行。"""
        return self._request("get_prompt", (name, arguments or {}), timeout=timeout)

    def close(self) -> None:
        """禁止新请求，等待已进入队列的工作结束后在原异步任务中关闭连接。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            loop = self._loop
            queue = self._queue
            if thread is None or not thread.is_alive():
                return
            if thread is current_thread():
                raise RuntimeError("Cannot close MCPBridge from its own thread")
            if loop is None or queue is None:
                raise RuntimeError("MCPBridge has not completed initialization")
            request = _Request("stop", ())
            loop.call_soon_threadsafe(queue.put_nowait, request)
        try:
            request.result.result(timeout=self._timeout)
        except FutureTimeout as error:
            raise TimeoutError("MCP worker did not stop in time") from error
        thread.join(timeout=self._timeout)
        if thread.is_alive():
            raise TimeoutError("MCP worker thread did not exit; verify server shutdown")

    def __enter__(self) -> "MCPBridge":
        """使用 with 语句时自动启动 MCP 连接。"""
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """离开 with 语句时统一关闭 MCP 连接。"""
        self.close()
