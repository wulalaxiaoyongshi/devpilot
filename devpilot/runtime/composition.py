"""V9.1：将现有 V5、V6、V7 和 V8 应用对象纳入同一个可关闭的生命周期容器。"""

from threading import RLock
from typing import Any


class DevPilotComposition:
    """只管理已经真实创建的应用对象，不伪造 V7/V8 绑定或自动替代业务操作。"""

    def __init__(self, core: Any, *, mcp: Any = None, workflow: Any = None, goal: Any = None):
        """核对组件归属和 V7/V8 连接关系，避免把不同仓库的运行时装到一起。"""
        if core is None or not all(callable(getattr(core, name, None)) for name in ("chat", "pump", "shutdown")):
            raise TypeError("core must provide chat, pump and shutdown")
        if mcp is not None and getattr(mcp, "app", None) is not core:
            raise ValueError("MCP app must wrap the same core app")
        if workflow is not None and not callable(getattr(workflow, "close", None)):
            raise TypeError("workflow must provide close")
        if goal is not None and getattr(goal, "workflow", None) is not workflow:
            raise ValueError("Goal app must reference the supplied WorkflowApplication")
        self.core = core
        self.mcp = mcp
        self.workflow = workflow
        self.goal = goal
        self._lock = RLock()
        self._closed = False
        self._started = False

    def start(self) -> None:
        """启动可选的 MCP 连接，没有配置 MCP 时保留原 V5 应用的启动方式。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("DevPilotComposition is closed")
            if self._started:
                return
            if self.mcp is not None:
                self.mcp.start()
            self._started = True

    def chat(self, text: str) -> str:
        """将用户消息发给原有真实 Lead Agent，若配置 MCP 则使用 V6 包装入口。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("DevPilotComposition is closed")
            if not self._started:
                self.start()
            target = self.mcp if self.mcp is not None else self.core
            return target.chat(text)

    def pump(self, resume_lead: bool = True) -> Any:
        """将后台事件投递交回 V5 LeadDelivery，避免在容器中重复实现事件逻辑。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("DevPilotComposition is closed")
            if not self._started:
                self.start()
            target = self.mcp if self.mcp is not None else self.core
            return target.pump(resume_lead=resume_lead)

    def capabilities(self) -> dict[str, bool]:
        """向未来 CLI 返回真实挂载的模块能力，不把未连接的 V7/V8 宣称为可用。"""
        return {"lead_v0_to_v5": True, "mcp_v6": self.mcp is not None,
                "workflow_v7": self.workflow is not None, "goal_v8": self.goal is not None}

    def close(self) -> None:
        """先结束工作流线程，再关闭 MCP 和 V5 后台任务，且避免重复释放核心应用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            errors: list[Exception] = []
            operations = []
            if self.workflow is not None:
                operations.append(self.workflow.close)
            operations.append(self.mcp.close if self.mcp is not None else self.core.shutdown)
            for operation in operations:
                try:
                    operation()
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("DevPilot runtime shutdown failures", errors)

    def __enter__(self) -> "DevPilotComposition":
        """进入上下文时启动已经配置的运行时连接。"""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """离开上下文时回收资源，并将业务异常保留给调用方处理。"""
        self.close()
        return False
