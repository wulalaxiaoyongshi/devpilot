"""描述外部后台任务的提交凭据、查询结果与可插拔查询接口。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal

from .store import json_snapshot


@dataclass(frozen=True)
class ExternalSubmission:
    """工作流动作提交后台任务时返回此对象，让 Engine 等到真正完成才继续执行。"""

    provider: str
    reference: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """检查提交凭据可以安全写入 JSON，且具有非空提供者和任务编号。"""
        if not self.provider.strip() or not self.reference.strip():
            raise ValueError("External provider and reference must not be empty")
        if not isinstance(self.metadata, dict):
            raise TypeError("External metadata must be a JSON object")
        json_snapshot(self.metadata)


@dataclass(frozen=True)
class ExternalOutcome:
    """查询一个外部任务时明确返回仍在运行、成功、失败或结果未知。"""

    status: Literal["running", "succeeded", "failed", "unknown"]
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        """拒绝错误的状态或非 JSON 输出，避免工作流错误推进。"""
        if self.status not in {"running", "succeeded", "failed", "unknown"}:
            raise ValueError("Invalid external status")
        if not isinstance(self.output, dict):
            raise TypeError("External output must be a JSON object")
        json_snapshot(self.output)


ExternalResolver = Callable[[str, dict[str, Any]], ExternalOutcome]


class ExternalResolverRegistry:
    """让 Workflow Engine 依据持久化的 provider 名称寻找外部任务状态查询器。"""

    def __init__(self) -> None:
        """建立提供者到查询函数的映射和进程内互斥锁。"""
        self._resolvers: dict[str, ExternalResolver] = {}
        self._lock = RLock()

    def register(self, provider: str, resolver: ExternalResolver) -> None:
        """宿主启动时为特定外部任务来源绑定唯一的状态查询函数。"""
        if not isinstance(provider, str) or not provider.strip() or not callable(resolver):
            raise ValueError("Valid provider and resolver are required")
        with self._lock:
            if provider in self._resolvers:
                raise ValueError(f"External resolver already registered: {provider}")
            self._resolvers[provider] = resolver

    def resolve(self, provider: str, reference: str, metadata: dict[str, Any]) -> ExternalOutcome:
        """Engine 轮询外部任务时向对应系统查询，而不自行猜测任务是否完成。"""
        with self._lock:
            resolver = self._resolvers.get(provider)
        if resolver is None:
            raise KeyError(f"External resolver not found: {provider}")
        result = resolver(reference, json_snapshot(metadata))
        if not isinstance(result, ExternalOutcome):
            raise TypeError("External resolver must return ExternalOutcome")
        return result
