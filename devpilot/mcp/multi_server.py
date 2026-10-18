"""V6.4：为多个 MCP Server 提供独立连接、稳定 V1 Tool、故障隔离和显式恢复。"""

import json
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Callable

from devpilot.mcp.adapter import MCPToolAdapter, v1_tool_name
from devpilot.mcp.results import MCPToolInfo, MCPToolResult
from devpilot.mcp.server_config import MCPServerDefinition
from devpilot.mcp.sync_bridge import MCPBridge


class ServerState(str, Enum):
    """描述每台 Server 的实际连接状态，而不是后台工具的业务成功或失败。"""

    STOPPED = "stopped"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    CLOSED = "closed"


class MCPConnectionErrors(RuntimeError):
    """汇总批量连接失败的 Server，以免其中一个故障掩盖其他结果。"""

    def __init__(self, failures: dict[str, str]):
        """根据各 Server 的失败原因生成批量连接异常。"""
        self.failures = dict(failures)
        super().__init__("MCP connection failures: " + ", ".join(sorted(failures)))


@dataclass(frozen=True)
class ServerStatus:
    """向宿主提供不包含环境变量及密钥的只读 Server 状态快照。"""

    alias: str
    state: ServerState
    generation: int
    tools: tuple[str, ...]
    last_error: str | None


class MCPToolSchemaChanged(RuntimeError):
    """重连后的可见工具或输入 Schema 已变化，必须显式重新审阅而不能静默替换。"""


class _SwitchableBridge:
    """V1 Handler 一直引用同一个代理，Server 重连时只替换代理背后的长连接。"""

    def __init__(self, alias: str, report_failure: Callable[[str, str, Any], None]):
        """创建可切换代理，并保存当前 Server 的故障报告回调。"""
        self.alias = alias
        self._bridge: Any | None = None
        self._tools: list[MCPToolInfo] = []
        self._lock = RLock()
        self._report_failure = report_failure

    def list_tools(self) -> list[MCPToolInfo]:
        """工具首次注册时返回已经预检过的发现快照，避免再次请求不可信 Server。"""
        with self._lock:
            return list(self._tools)

    def swap(self, new_bridge: Any, tools: list[MCPToolInfo]) -> Any | None:
        """重连成功后原子切换到新连接，并返回应由管理器关闭的旧连接。"""
        with self._lock:
            old = self._bridge
            self._tools = list(tools)
            self._bridge = new_bridge
            return old

    def detach(self) -> Any | None:
        """停止某台 Server 时先让新工具调用失败，再交回旧连接供关闭。"""
        with self._lock:
            old = self._bridge
            self._bridge = None
            return old

    def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """V1 Tool Handler 调用时转发至当前 Server，传输异常只影响该 Server 的状态。"""
        try:
            with self._lock:
                bridge = self._bridge
                if bridge is None:
                    raise RuntimeError(f"MCP Server {self.alias} is not connected")
                return bridge.call_tool(name, arguments)
        except (RuntimeError, OSError, TimeoutError) as error:
            if bridge is not None:
                self._report_failure(self.alias, f"{type(error).__name__}: {error}", bridge)
            raise


    def _content_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """读取 Resource 或 Prompt 时沿用可切换连接与故障隔离逻辑，重连后不保留旧连接。"""
        bridge = None
        try:
            with self._lock:
                bridge = self._bridge
                if bridge is None:
                    raise RuntimeError(f"MCP Server {self.alias} is not connected")
                return getattr(bridge, method)(*args, **kwargs)
        except (RuntimeError, OSError, TimeoutError) as error:
            if bridge is not None:
                self._report_failure(self.alias, f"{type(error).__name__}: {error}", bridge)
            raise

    def list_resources(self, cursor: str | None = None):
        """宿主列举 Resources 时向这台 Server 当前连接转发分页查询。"""
        return self._content_call("list_resources", cursor=cursor)

    def list_resource_templates(self, cursor: str | None = None):
        """宿主查询资源 URI 模板时使用当前可用连接而不重新建立会话。"""
        return self._content_call("list_resource_templates", cursor=cursor)

    def read_resource(self, uri: str):
        """宿主通过已批准的 URI 获取资源时只向相应 Server 转发请求。"""
        return self._content_call("read_resource", uri)

    def list_prompts(self, cursor: str | None = None):
        """宿主列举 Prompt 模板时复用工具所使用的同一台 MCP Server。"""
        return self._content_call("list_prompts", cursor=cursor)

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None):
        """宿主获取已批准的 Prompt 模板时使用可切换连接完成读取。"""
        return self._content_call("get_prompt", name, arguments or {})


@dataclass
class _ServerSlot:
    """保存 Server 的稳定代理、首次工具快照和当前连接状态。"""

    definition: MCPServerDefinition
    proxy: _SwitchableBridge
    state: ServerState = ServerState.STOPPED
    generation: int = 0
    tools: tuple[str, ...] = ()
    fingerprint: tuple[tuple[str, str], ...] | None = None
    last_error: str | None = None


def _selected_tools(discovered: list[MCPToolInfo], definition: MCPServerDefinition) -> tuple[list[MCPToolInfo], tuple[str, ...], tuple[tuple[str, str], ...]]:
    """对发现结果应用工具白名单、数量上限与去重检查，得到重连时可比较的 Schema 指纹。"""
    if len(discovered) > definition.max_tools:
        raise ValueError(f"Server {definition.alias} exposes too many tools: {len(discovered)}")
    selected = [tool for tool in discovered if definition.allow_tools is None or tool.name in definition.allow_tools]
    names = tuple(v1_tool_name(definition.alias, tool.name) for tool in selected)
    if len(set(names)) != len(names):
        raise ValueError(f"Server {definition.alias} returned duplicate tool names")
    if definition.allow_tools is not None:
        absent = definition.allow_tools - {tool.name for tool in selected}
        if absent:
            raise ValueError(f"Server {definition.alias} is missing approved tools: {', '.join(sorted(absent))}")
    fingerprint = tuple(sorted((tool.name, json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True)) for tool in selected))
    return selected, names, fingerprint


class MCPMultiServerManager:
    """管理多个本地或 HTTP MCP Server，注册一次 V1 Tool 并在重连后继续复用同一 Handler。"""

    def __init__(self, registry: Any, definitions: list[MCPServerDefinition], bridge_factory: Callable[[MCPServerDefinition], Any] | None = None, max_servers: int = 16):
        """宿主启动时绑定已有 V1 Registry、验证别名，并为各 Server 准备独立状态。"""
        if not 1 <= len(definitions) <= max_servers:
            raise ValueError("Server count must be between 1 and max_servers")
        self.registry = registry
        self._lock = RLock()
        self._closed = False
        self._bridge_factory = bridge_factory or (lambda definition: MCPBridge.from_http(definition.remote, timeout=definition.timeout_seconds) if definition.remote is not None else MCPBridge.from_local(definition.local, timeout=definition.timeout_seconds))
        self._slots: dict[str, _ServerSlot] = {}
        for definition in definitions:
            if definition.alias in self._slots:
                raise ValueError(f"Duplicate MCP Server alias: {definition.alias}")
            proxy = _SwitchableBridge(definition.alias, self._report_failure)
            self._slots[definition.alias] = _ServerSlot(definition=definition, proxy=proxy)

    def _slot(self, alias: str) -> _ServerSlot:
        """找到指定 Server 的状态槽，未知别名直接报错以防操作错误目标。"""
        try:
            return self._slots[alias]
        except KeyError as error:
            raise KeyError(f"Unknown MCP Server: {alias}") from error

    def _report_failure(self, alias: str, reason: str, bridge: Any) -> None:
        """某台 Server 的工具遇到传输异常时只标记它为降级，不中断其他连接。"""
        with self._lock:
            slot = self._slot(alias)
            with slot.proxy._lock:
                active = slot.proxy._bridge is bridge
            if active and slot.state != ServerState.CLOSED:
                slot.state = ServerState.DEGRADED
                slot.last_error = reason.split(":", 1)[0] + ": MCP call failed; see local diagnostic logs"

    def _status(self, slot: _ServerSlot) -> ServerStatus:
        """将内部可变 Server 状态转成不包含密钥的只读快照。"""
        return ServerStatus(slot.definition.alias, slot.state, slot.generation, slot.tools, slot.last_error)

    def status(self, alias: str) -> ServerStatus:
        """宿主查询一台 Server 时返回当前连接状态、代数和已注册的工具名。"""
        with self._lock:
            return self._status(self._slot(alias))

    def statuses(self) -> dict[str, ServerStatus]:
        """宿主展示运行状态时一次性获取所有 Server 的独立状态快照。"""
        with self._lock:
            return {alias: self._status(slot) for alias, slot in self._slots.items()}

    def content_bridge(self, alias: str) -> _SwitchableBridge:
        """宿主读取外部内容时获得指定 Server 的稳定代理，权限仍由 MCPContentRuntime 检查。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("MCPMultiServerManager is closed")
            return self._slot(alias).proxy

    def connect(self, alias: str) -> ServerStatus:
        """首次连接时注册工具，后续连接校验 Schema 后仅替换代理背后的连接。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("MCPMultiServerManager is closed")
            slot = self._slot(alias)
            if slot.state == ServerState.CONNECTED:
                return self._status(slot)
            bridge = self._bridge_factory(slot.definition)
            try:
                bridge.start()
                selected, names, fingerprint = _selected_tools(bridge.list_tools(), slot.definition)
                if slot.fingerprint is None:
                    adapter = MCPToolAdapter(slot.proxy, alias)
                    slot.proxy._tools = selected
                    try:
                        registered = tuple(adapter.register(self.registry, allow_tools=None, max_tools=slot.definition.max_tools))
                    finally:
                        slot.proxy._tools = []
                    if registered != names:
                        raise RuntimeError("Registered MCP tools differ from discovered tools")
                    slot.fingerprint = fingerprint
                    slot.tools = names
                elif slot.fingerprint != fingerprint:
                    raise MCPToolSchemaChanged(f"MCP Server {alias} changed its advertised tool schema; review before enabling")
                old = slot.proxy.swap(bridge, selected)
                slot.generation += 1
                slot.state = ServerState.CONNECTED
                slot.last_error = None
                if old is not None:
                    try:
                        old.close()
                    except Exception as error:
                        slot.last_error = f"Old MCP connection cleanup failed: {type(error).__name__}"
                return self._status(slot)
            except BaseException as error:
                if isinstance(error, MCPToolSchemaChanged):
                    old = slot.proxy.detach()
                    if old is not None:
                        try:
                            old.close()
                        except Exception:
                            pass
                if slot.state != ServerState.CONNECTED or isinstance(error, MCPToolSchemaChanged):
                    slot.state = ServerState.DEGRADED
                slot.last_error = f"{type(error).__name__}: MCP connection or schema verification failed"
                try:
                    bridge.close()
                except Exception:
                    pass
                raise

    def reconnect(self, alias: str) -> ServerStatus:
        """宿主确认需要恢复某台 Server 时显式重新建连，不重复注册工具也不重放失败调用。"""
        with self._lock:
            slot = self._slot(alias)
            if slot.state == ServerState.CONNECTED:
                slot.state = ServerState.DEGRADED
            return self.connect(alias)

    def disconnect(self, alias: str) -> ServerStatus:
        """宿主停止指定 Server 时断开其连接，但保留原工具名供受控重连复用。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("MCPMultiServerManager is closed")
            slot = self._slot(alias)
            bridge = slot.proxy.detach()
            slot.state = ServerState.STOPPED
            slot.last_error = None
            if bridge is not None:
                bridge.close()
            return self._status(slot)

    def check_health(self, alias: str) -> ServerStatus:
        """宿主进行主动健康检查时重新发现工具，验证连接可用且 Schema 未悄悄改变。"""
        with self._lock:
            slot = self._slot(alias)
            with slot.proxy._lock:
                bridge = slot.proxy._bridge
                if bridge is None:
                    raise RuntimeError(f"MCP Server {alias} is not connected")
                try:
                    _, _, fingerprint = _selected_tools(bridge.list_tools(), slot.definition)
                    if fingerprint != slot.fingerprint:
                        raise MCPToolSchemaChanged(f"MCP Server {alias} changed its tool schema")
                except Exception as error:
                    old = slot.proxy.detach() if isinstance(error, MCPToolSchemaChanged) else None
                    if old is not None:
                        try:
                            old.close()
                        except Exception:
                            pass
                    slot.state = ServerState.DEGRADED
                    slot.last_error = f"{type(error).__name__}: MCP connection or schema verification failed"
                    raise
            slot.state = ServerState.CONNECTED
            slot.last_error = None
            return self._status(slot)

    def connect_all(self, strict: bool = False) -> dict[str, ServerStatus]:
        """启动时逐台连接所有 Server，默认收集故障继续运行，严格模式则在最后统一报错。"""
        failures: dict[str, str] = {}
        for alias in tuple(self._slots):
            try:
                self.connect(alias)
            except Exception as error:
                failures[alias] = type(error).__name__
        if strict and failures:
            raise MCPConnectionErrors(failures)
        return self.statuses()

    def close(self) -> None:
        """应用退出时从后往前关闭全部连接，并让已注册 Handler 不再执行远端调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            errors: list[str] = []
            for alias in reversed(tuple(self._slots)):
                slot = self._slots[alias]
                bridge = slot.proxy.detach()
                slot.state = ServerState.CLOSED
                if bridge is not None:
                    try:
                        bridge.close()
                    except Exception as error:
                        errors.append(f"{alias}: {type(error).__name__}: {error}")
            if errors:
                raise RuntimeError("MCP shutdown encountered errors: " + "; ".join(errors))

    def __enter__(self) -> "MCPMultiServerManager":
        """宿主使用 with 时直接返回管理器，连接动作由 connect_all 显式控制。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出 with 时关闭所有连接，避免留下 MCP 线程和子进程。"""
        self.close()
