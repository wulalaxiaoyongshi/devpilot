"""管理 Workflow 步骤名称与实际 Python 执行函数之间的映射。"""

from collections.abc import Callable
from threading import RLock

from .model import StepContext
from .external import ExternalSubmission

WorkflowAction = Callable[[StepContext], dict | ExternalSubmission]


class ActionRegistry:
    """让 Workflow Engine 只认识动作名称，而不直接依赖 V1～V6 的具体实现。"""

    def __init__(self):
        """创建空动作表和用于注册操作的线程锁。"""
        self._actions: dict[str, WorkflowAction] = {}
        self._lock = RLock()

    def register(self, name: str, handler: WorkflowAction) -> None:
        """将可信的动作函数绑定到一个唯一名称，供 Workflow 步骤执行时查找。"""
        if not isinstance(name, str) or not name.strip() or not callable(handler):
            raise ValueError("Action name and callable handler are required")
        with self._lock:
            if name in self._actions:
                raise ValueError(f"Action already registered: {name}")
            self._actions[name] = handler

    def get(self, name: str) -> WorkflowAction:
        """根据步骤定义里的动作名称取得对应处理函数，找不到就明确报错。"""
        with self._lock:
            try:
                return self._actions[name]
            except KeyError as exc:
                raise KeyError(f"Workflow action not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        """列出当前进程已经注册的全部动作名称，便于启动前检查。"""
        with self._lock:
            return tuple(sorted(self._actions))
