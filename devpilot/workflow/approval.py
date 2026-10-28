"""定义工作流审批的授权规则、审批单及不可随意覆盖的审计事件。"""

from dataclasses import dataclass
from typing import Literal

ApprovalStatus = Literal["waiting", "approved", "rejected", "expired"]
ApprovalEventType = Literal["requested", "approved", "rejected", "expired"]


@dataclass(frozen=True)
class ApprovalPolicy:
    """指定哪些可信身份可以审批、审批多久过期以及拒绝时是否必须说明原因。"""

    approvers: tuple[str, ...]
    timeout_seconds: int | None = None
    requester: str | None = None
    allow_self_approval: bool = False
    require_rejection_reason: bool = True
    purpose: str = ""

    def __post_init__(self) -> None:
        """拒绝空审批名单、重复身份和非法过期时间，避免生成无法安全执行的审批单。"""
        if not self.approvers or any(not isinstance(p, str) or not p.strip() for p in self.approvers):
            raise ValueError("Approval requires at least one nonempty approver")
        if len(set(self.approvers)) != len(self.approvers):
            raise ValueError("Duplicate approver")
        if self.timeout_seconds is not None and (type(self.timeout_seconds) is not int or self.timeout_seconds < 1):
            raise ValueError("Approval timeout_seconds must be a positive integer")
        if self.requester is not None and (not isinstance(self.requester, str) or not self.requester.strip()):
            raise ValueError("Approval requester must be a nonempty identity")
        if type(self.allow_self_approval) is not bool or type(self.require_rejection_reason) is not bool:
            raise ValueError("Approval policy flags must be boolean")
        if not isinstance(self.purpose, str):
            raise ValueError("Approval purpose must be a string")


@dataclass
class ApprovalRequest:
    """保存单次审批请求的编号、有效期、当前决定以及作决定的可信身份。"""

    request_id: str
    status: ApprovalStatus
    requested_at: str
    expires_at: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    note: str = ""


@dataclass(frozen=True)
class ApprovalEvent:
    """保存一条审批历史事件，便于审计每次申请、批准、拒绝或过期。"""

    request_id: str
    event: ApprovalEventType
    occurred_at: str
    actor: str
    note: str = ""
