"""定义 V8.1 目标、验收条件、状态和记录的数据类型。"""

from dataclasses import dataclass, field
from typing import Any, Literal


CriterionKind = Literal["command_exit_code", "file_exists", "file_contains", "git_head", "manual"]
CriterionStatus = Literal["unverified", "pass", "fail", "unknown"]
GoalStatus = Literal["pending", "executing", "verifying", "unknown", "blocked", "repairing", "verified", "exhausted"]


@dataclass(frozen=True)
class AcceptanceCriterion:
    """定义一个具体的验收条件，但本阶段不会执行它。"""

    id: str
    description: str
    kind: CriterionKind
    config: dict[str, Any] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class GoalSpec:
    """把用户目标和多条验收条件保存成一份可检查的目标蓝图。"""

    id: str
    title: str
    description: str
    criteria: tuple[AcceptanceCriterion, ...]


@dataclass
class CriterionRecord:
    """记录某条验收条件的核验状态，未来由 V8.3 验证器填写结果。"""

    status: CriterionStatus = "unverified"
    evidence_ids: list[str] = field(default_factory=list)
    checked_at: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class GoalEvent:
    """记录一次状态变化或备注的发生时间、操作者和说明。"""

    at: str
    event_type: str
    actor: str
    note: str
    from_status: str
    to_status: str


@dataclass
class GoalRun:
    """保存一次目标运行的蓝图快照、每条验收状态和全部状态事件。"""

    id: str
    spec: GoalSpec
    inputs: dict[str, Any]
    criteria: dict[str, CriterionRecord]
    status: GoalStatus = "pending"
    events: list[GoalEvent] = field(default_factory=list)
    version: int = 0
    created_at: str = ""
    updated_at: str = ""
