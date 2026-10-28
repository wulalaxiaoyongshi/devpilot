"""定义工作流、步骤、条件以及运行状态的数据结构。"""

from dataclasses import dataclass, field
from typing import Any, Literal

from .approval import ApprovalEvent, ApprovalPolicy, ApprovalRequest
from .recovery import RecoveryEvent, RetryPolicy

StepKind = Literal["action", "approval"]
StepStatus = Literal["pending", "running", "succeeded", "failed", "skipped", "waiting_approval", "waiting_external", "rejected", "approval_expired", "retry_wait", "recovery_required"]
RunStatus = Literal["running", "waiting_approval", "waiting_external", "completed", "failed", "rejected", "approval_expired", "retry_wait", "recovery_required"]


@dataclass(frozen=True)
class StepCondition:
    """通过上游步骤输出中的一个字段判断当前步骤是否需要运行。"""

    step_id: str
    field: str
    equals: Any


@dataclass(frozen=True)
class StepDefinition:
    """描述一步工作如何执行、依赖谁、何时跳过以及最多允许尝试几次。"""

    id: str
    kind: StepKind = "action"
    action: str | None = None
    depends_on: tuple[str, ...] = ()
    allow_skipped_dependencies: tuple[str, ...] = ()
    condition: StepCondition | None = None
    params: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    approval: ApprovalPolicy | None = None
    retry: RetryPolicy | None = None
    exclusive_resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDefinition:
    """将多条步骤定义组成一份可检查、可保存的工作流蓝图。"""

    id: str
    name: str
    steps: tuple[StepDefinition, ...]


@dataclass
class StepState:
    """记录某个步骤当前执行到哪里、已经执行几次和执行结果。"""

    status: StepStatus = "pending"
    attempts: int = 0
    output: dict[str, Any] | None = None
    error: str | None = None
    note: str | None = None
    approval_request: ApprovalRequest | None = None
    approval_events: list[ApprovalEvent] = field(default_factory=list)
    attempt_id: str | None = None
    retry_at: str | None = None
    external_submitted_at: str | None = None
    recovery_events: list[RecoveryEvent] = field(default_factory=list)


@dataclass
class WorkflowRun:
    """保存一次 Workflow 的蓝图快照、输入、步骤状态以及整体状态。"""

    id: str
    definition: WorkflowDefinition
    inputs: dict[str, Any]
    steps: dict[str, StepState]
    status: RunStatus = "running"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class StepContext:
    """向动作处理函数提供本步骤参数、工作流输入和已完成步骤的输出。"""

    run_id: str
    workflow_id: str
    step_id: str
    attempt: int
    inputs: dict[str, Any]
    params: dict[str, Any]
    results: dict[str, dict[str, Any]]
    attempt_id: str = ""
    idempotency_key: str = ""
