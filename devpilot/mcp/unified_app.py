"""V6.6：将 V6 MCP 多服务能力挂载到原来的 V5.7 DevPilotApp。"""

from collections.abc import Callable
from inspect import getsource
from pathlib import Path
from threading import RLock
from typing import Any

from devpilot.mcp.content_runtime import MCPContentRuntime
from devpilot.mcp.multi_server import MCPMultiServerManager, ServerStatus
from devpilot.mcp.server_config import MCPServerDefinition, load_server_definitions


class UnifiedMCPApp:
    """让 V5.7 App 继续管理 Agent，而 V6 专门管理外部 MCP Server 的生命周期。"""

    def __init__(self, app: Any, definitions: list[MCPServerDefinition], bridge_factory: Callable | None = None):
        """把真实 Lead 的 V1 ToolRegistry 交给 V6.4，并共享 V5.7 的应用生命周期。"""
        if not definitions:
            raise ValueError("At least one MCP Server must be configured")
        if not hasattr(app, "lead") or not hasattr(app.lead, "tools"):
            raise TypeError("V5.7 App must provide app.lead.tools (V1 ToolRegistry)")
        self.app = app
        self.mcp = MCPMultiServerManager(app.lead.tools, definitions, bridge_factory=bridge_factory)
        self._lock = RLock()
        self._started = False
        self._closed = False

    def start(self, strict: bool = True) -> dict[str, ServerStatus]:
        """宿主启动时连接所有允许的 MCP Server，严格模式发现故障便拒绝启动整个集成。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("UnifiedMCPApp is closed")
            if self._started:
                return self.mcp.statuses()
            try:
                statuses = self.mcp.connect_all(strict=strict)
            except BaseException:
                self.close()
                raise
            self._started = True
            return statuses

    def chat(self, user_input: str) -> str:
        """用户发来指令时仍调用真正的 V5.7 Lead Agent，让它通过已有 V1 Dispatcher 使用 MCP 工具。"""
        with self._lock:
            self._require_started()
            return self.app.chat(user_input)

    def pump(self, resume_lead: bool = True) -> Any:
        """后台任务完成时仍使用 V5.7 原有事件泵，让 Lead 的下一轮推理继续看到 MCP 工具。"""
        with self._lock:
            self._require_started()
            return self.app.pump(resume_lead=resume_lead)

    def content(self, alias: str, *, allowed_uri_prefixes: tuple[str, ...] = (), allowed_prompts: frozenset[str] = frozenset(), max_chars: int = 12000) -> MCPContentRuntime:
        """宿主明确选择一台 Server 和允许的 URI/Prompt 后，取得受限内容读取接口而非 Agent 工具。"""
        with self._lock:
            self._require_started()
            return MCPContentRuntime(self.mcp.content_bridge(alias), allowed_uri_prefixes=allowed_uri_prefixes, allowed_prompts=allowed_prompts, max_chars=max_chars)

    def statuses(self) -> dict[str, ServerStatus]:
        """运维或 CLI 查看多台 Server 运行情况时返回不包含密钥的连接状态快照。"""
        return self.mcp.statuses()

    def close(self) -> None:
        """宿主退出时先关闭 MCP 连接，再关闭 V5 后台任务与定时器，并确保重复调用无副作用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            errors: list[Exception] = []
            for shutdown in (self.mcp.close, self.app.shutdown):
                try:
                    shutdown()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("V6.6 shutdown failures", errors)

    def _require_started(self) -> None:
        """任何 MCP 相关业务操作开始前检查运行时是否已经正确连接且没有被关闭。"""
        if not self._started or self._closed:
            raise RuntimeError("UnifiedMCPApp must be started and not closed")

    def __enter__(self) -> "UnifiedMCPApp":
        """宿主进入 with 时启动已配置的 MCP Server 并返回当前应用。"""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """宿主离开 with 时回收 MCP Server、Worker 线程与事件调度资源。"""
        self.close()


def create_mcp_app(repo_root: Path, model_factory: Callable[[str], Any], config_path: Path, approve: Callable[[str, dict], bool] | None = None, *, state_dir: Path | None = None, strict: bool = True, max_workers: int = 4) -> UnifiedMCPApp:
    """在程序启动时复用 V5.7 的 create_app，再读取可信 MCP 配置并接入 Lead 的工具注册表。"""
    from devpilot.runtime.factory import create_app

    source = getsource(create_app)
    if "MCPPermissionPolicy" not in source or "register_pre_tool_use(policy.check)" not in source:
        raise RuntimeError("V5.7 Factory is missing V6.2 MCP Permission wiring; run tools/patch_v6_2_factory.py after reviewing the source")
    definitions = load_server_definitions(config_path)
    if not definitions:
        raise ValueError("MCP configuration has no servers")
    app = create_app(repo_root, model_factory, approve=approve, state_dir=state_dir, max_workers=max_workers)
    unified = UnifiedMCPApp(app, definitions)
    unified.start(strict=strict)
    return unified
