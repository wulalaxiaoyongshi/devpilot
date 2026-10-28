"""定义工作流的安全重试策略、未知结果异常和恢复审计事件。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RetryPolicy:
    """为单个步骤声明是否允许自动重试、重试延迟与外部任务等待上限。"""

    automatic_on_transient: bool = False
    idempotent: bool = False
    delay_seconds: float = 0
    external_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        """拒绝缺少幂等承诺的自动重试策略和不合法的时间设置。"""
        if type(self.automatic_on_transient) is not bool or type(self.idempotent) is not bool:
            raise TypeError("Retry flags must be booleans")
        if self.automatic_on_transient and not self.idempotent:
            raise ValueError("Automatic retry requires an explicitly idempotent action")
        if type(self.delay_seconds) not in (int, float) or not 0 <= self.delay_seconds < float("inf"):
            raise ValueError("delay_seconds must be finite and non-negative")
        if self.external_timeout_seconds is not None and (
            type(self.external_timeout_seconds) not in (int, float)
            or not 0 < self.external_timeout_seconds < float("inf")
        ):
            raise ValueError("external_timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class RecoveryEvent:
    """记录步骤何时因什么原因进入重试或恢复状态，以及是谁确认的。"""

    event: str
    at: str
    actor: str
    evidence: str = ""
    attempt_id: str | None = None


class RetryableActionError(Exception):
    """表示动作明确失败且被宿主确认在幂等策略下可安全重试。"""


class UncertainActionError(Exception):
    """表示工具调用后结果未知，必须先核实远端状态，不能直接重试。"""


ReconciliationOutcome = Literal["succeeded", "not_executed", "failed", "still_running"]
