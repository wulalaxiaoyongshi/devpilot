"""执行顺序明确、支持条件分支与恢复的单进程同步 Workflow Engine。"""

import re
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from threading import RLock
from uuid import uuid4

from .actions import ActionRegistry
from .approval import ApprovalEvent, ApprovalPolicy, ApprovalRequest
from .external import ExternalResolverRegistry, ExternalSubmission
from .model import StepContext, StepDefinition, StepState, WorkflowDefinition, WorkflowRun
from .recovery import RecoveryEvent, RetryableActionError, UncertainActionError, RetryPolicy, ReconciliationOutcome
from .store import WorkflowStore, json_snapshot

_STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _utc_now() -> str:
    """生成可以排序和持久化的 UTC 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def validate_definition(definition: WorkflowDefinition) -> None:
    """验证步骤 ID、动作类型、依赖引用、分支条件和 DAG 是否有效。"""
    if not definition.id or not definition.name or not definition.steps:
        raise ValueError("Workflow needs id, name and at least one step")
    steps = {step.id: step for step in definition.steps}
    if len(steps) != len(definition.steps):
        raise ValueError("Duplicated step ID")
    for step in definition.steps:
        if not _STEP_ID_PATTERN.fullmatch(step.id):
            raise ValueError(f"Invalid step ID: {step.id}")
        if step.kind not in {"action", "approval"}:
            raise ValueError(f"Invalid step kind: {step.kind}")
        if (step.kind == "action") != bool(step.action):
            raise ValueError(f"Action step must have an action and approval step must not: {step.id}")
        if step.approval is not None and (step.kind != "approval" or not isinstance(step.approval, ApprovalPolicy)):
            raise ValueError(f"Only approval steps can have a valid approval policy: {step.id}")
        if type(step.max_attempts) is not int or step.max_attempts < 1:
            raise ValueError(f"max_attempts must be positive: {step.id}")
        if step.retry is not None and (step.kind != "action" or not isinstance(step.retry, RetryPolicy)):
            raise ValueError(f"Only action steps can have a retry policy: {step.id}")
        if step.exclusive_resources and step.kind != "action":
            raise ValueError(f"Only action steps can hold exclusive resources: {step.id}")
        if any(type(name) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", name) for name in step.exclusive_resources):
            raise ValueError(f"Invalid exclusive resource name: {step.id}")
        if len(set(step.exclusive_resources)) != len(step.exclusive_resources):
            raise ValueError(f"Duplicated exclusive resource: {step.id}")
        if len(set(step.depends_on)) != len(step.depends_on):
            raise ValueError(f"Duplicated dependencies in {step.id}")
        if not set(step.allow_skipped_dependencies).issubset(step.depends_on):
            raise ValueError(f"Skipped dependencies must also be normal dependencies: {step.id}")
        if any(parent not in steps for parent in step.depends_on):
            raise ValueError(f"Unknown dependency in {step.id}")
        if step.condition is not None:
            if step.condition.step_id not in step.depends_on or not step.condition.field:
                raise ValueError(f"Condition must refer to a direct dependency: {step.id}")
        json_snapshot(step.params)
        if step.condition is not None:
            json_snapshot(step.condition.equals)
    marks: dict[str, int] = {}

    def visit(step_id: str) -> None:
        """通过深度优先遍历识别循环依赖，避免工作流永远无法开始。"""
        mark = marks.get(step_id, 0)
        if mark == 1:
            raise ValueError("Workflow has a cycle")
        if mark == 2:
            return
        marks[step_id] = 1
        for parent in steps[step_id].depends_on:
            visit(parent)
        marks[step_id] = 2

    for step_id in steps:
        visit(step_id)


class WorkflowEngine:
    """根据步骤依赖依次执行动作，并将每次状态变化落盘以支持故障分析与人工恢复。"""

    def __init__(self, store: WorkflowStore, actions: ActionRegistry, external: ExternalResolverRegistry | None = None, *, clock: Callable[[], datetime] | None = None):
        """接入状态存储和动作注册表，并创建本进程的串行调度锁。"""
        self.store = store
        self.actions = actions
        self.external = external or ExternalResolverRegistry()
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def start(self, definition: WorkflowDefinition, inputs: dict | None = None) -> WorkflowRun:
        """验证工作流蓝图和输入，创建一份状态全为 pending 的持久运行实例。"""
        with self._lock:
            validate_definition(definition)
            safe_inputs = json_snapshot(inputs if inputs is not None else {})
            if not isinstance(safe_inputs, dict):
                raise TypeError("Workflow input must be a JSON object")
            run = WorkflowRun(
                id=uuid4().hex, definition=definition, inputs=safe_inputs,
                steps={step.id: StepState() for step in definition.steps},
                created_at=_utc_now(), updated_at=_utc_now(),
            )
            self.store.create(run)
            return self.store.get(run.id)

    def _approval_now(self) -> datetime:
        """从宿主提供的时钟读取带时区的当前时间，方便可靠地判断审批是否过期。"""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Approval clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def _open_approval(self, run: WorkflowRun, step: StepDefinition) -> None:
        """为刚进入审批关卡的步骤创建唯一请求编号并保存一条申请事件。"""
        now = self._approval_now()
        expires = now + timedelta(seconds=step.approval.timeout_seconds) if step.approval and step.approval.timeout_seconds else None
        state = run.steps[step.id]
        request = ApprovalRequest(uuid4().hex, "waiting", now.isoformat(), expires.isoformat() if expires else None)
        state.approval_request = request
        state.approval_events.append(ApprovalEvent(request.request_id, "requested", now.isoformat(), "workflow", step.approval.purpose if step.approval else ""))
        state.status = "waiting_approval"
        self._save(run)

    def _expire_approval(self, run: WorkflowRun, step_id: str, now: datetime) -> bool:
        """检查指定待审批步骤是否到期，到期则记录事件并终止当前审批请求。"""
        state = run.steps[step_id]
        request = state.approval_request
        if state.status != "waiting_approval" or request is None or request.expires_at is None:
            return False
        if now < datetime.fromisoformat(request.expires_at):
            return False
        request.status = "expired"
        request.decided_at = now.isoformat()
        request.decided_by = "workflow"
        state.status = "approval_expired"
        state.note = "Approval expired; host must explicitly reissue before proceeding"
        state.approval_events.append(ApprovalEvent(request.request_id, "expired", now.isoformat(), "workflow"))
        self._save(run)
        return True

    def get_approval_request(self, run_id: str, step_id: str) -> ApprovalRequest:
        """让可信宿主读取指定步骤当前审批单，向被授权的审批人展示请求编号和截止时间。"""
        with self._lock:
            run = self.store.get(run_id)
            state = run.steps.get(step_id)
            if state is None or state.approval_request is None:
                raise ValueError("Step has no approval request")
            return state.approval_request

    def expire_approvals(self, run_id: str) -> WorkflowRun:
        """由宿主轮询待审批工作流并将所有到期审批持久化标记为过期。"""
        with self._lock:
            run = self.store.get(run_id)
            now = self._approval_now()
            for step in run.definition.steps:
                if self._expire_approval(run, step.id, now):
                    run = self.store.get(run_id)
            return self.store.get(run_id)

    def reissue_approval(self, run_id: str, step_id: str) -> WorkflowRun:
        """由可信宿主对已过期审批重新发单，保留旧审计记录并生成新的请求编号。"""
        with self._lock:
            run = self.store.get(run_id)
            step = next((item for item in run.definition.steps if item.id == step_id), None)
            if step is None or step.kind != "approval" or run.steps[step_id].status != "approval_expired":
                raise ValueError("Only an expired approval can be reissued")
            self._open_approval(run, step)
            return self.store.get(run_id)

    def _refresh_status(self, run: WorkflowRun) -> None:
        """根据全部步骤状态重新计算 Workflow 是运行、等待、失败还是完成。"""
        statuses = {state.status for state in run.steps.values()}
        if "rejected" in statuses:
            run.status = "rejected"
        elif "approval_expired" in statuses:
            run.status = "approval_expired"
        elif "recovery_required" in statuses:
            run.status = "recovery_required"
        elif "failed" in statuses:
            run.status = "failed"
        elif "waiting_approval" in statuses:
            run.status = "waiting_approval"
        elif "waiting_external" in statuses:
            run.status = "waiting_external"
        elif "retry_wait" in statuses:
            run.status = "retry_wait"
        elif statuses.issubset({"succeeded", "skipped"}):
            run.status = "completed"
        else:
            run.status = "running"
        run.updated_at = _utc_now()

    def _save(self, run: WorkflowRun) -> None:
        """先更新整体状态再保存 Workflow，确保磁盘状态和步骤状态一致。"""
        self._refresh_status(run)
        self.store.save(run)

    def _candidate(self, run: WorkflowRun) -> tuple[StepDefinition, str] | None:
        """从步骤定义顺序中选出首个依赖已经满足或必须跳过的 pending 步骤。"""
        for step in run.definition.steps:
            state = run.steps[step.id]
            if state.status != "pending":
                continue
            skipped_required = [
                parent for parent in step.depends_on
                if run.steps[parent].status == "skipped" and parent not in step.allow_skipped_dependencies
            ]
            if skipped_required:
                return step, "skip"
            ready = all(
                run.steps[parent].status == "succeeded"
                or (run.steps[parent].status == "skipped" and parent in step.allow_skipped_dependencies)
                for parent in step.depends_on
            )
            if ready:
                return step, "ready"
        return None

    def _matches_condition(self, run: WorkflowRun, step: StepDefinition) -> bool:
        """读取一个已完成上游步骤的指定输出字段，判断是否符合当前分支条件。"""
        condition = step.condition
        if condition is None:
            return True
        source = run.steps[condition.step_id]
        if source.status != "succeeded" or source.output is None:
            return False
        return source.output.get(condition.field, object()) == condition.equals

    def _record_recovery(self, state: StepState, event: str, actor: str, evidence: str = "") -> None:
        """给当前步骤追加一条包含尝试编号和时间的恢复审计记录。"""
        state.recovery_events.append(RecoveryEvent(event, self._approval_now().isoformat(), actor, evidence, state.attempt_id))

    def _needs_reconciliation(self, state: StepState, reason: str) -> None:
        """将执行结果未知的步骤停在待核实状态并持久保留原始凭据。"""
        state.status = "recovery_required"
        state.error = reason
        state.retry_at = None
        self._record_recovery(state, "reconciliation_required", "workflow", reason)

    def _schedule_safe_retry(self, state: StepState, step: StepDefinition, error: Exception) -> bool:
        """仅在明确声明幂等、捕获可重试错误且尚有次数时设置下一次执行时间。"""
        policy = step.retry
        if policy is None or not policy.automatic_on_transient or not policy.idempotent or state.attempts >= step.max_attempts:
            return False
        when = self._approval_now() + timedelta(seconds=policy.delay_seconds)
        state.status = "retry_wait"
        state.retry_at = when.isoformat()
        state.error = f"{type(error).__name__}: {error}"
        self._record_recovery(state, "retry_scheduled", "workflow", state.error)
        return True

    def _wake_due_retries(self, run: WorkflowRun) -> bool:
        """把已经到期的安全重试步骤改回待执行，并保留原来的尝试次数。"""
        changed = False
        now = self._approval_now()
        for state in run.steps.values():
            if state.status == "retry_wait" and state.retry_at and now >= datetime.fromisoformat(state.retry_at):
                state.status = "pending"
                state.retry_at = None
                self._record_recovery(state, "retry_due", "workflow")
                changed = True
        if changed:
            self._save(run)
        return changed

    def check_external_timeouts(self, run_id: str) -> WorkflowRun:
        """将超过等待期限的外部 Job 标为需要核实，而不取消或重复提交原任务。"""
        with self._lock:
            run = self.store.get(run_id)
            changed = False
            now = self._approval_now()
            for step in run.definition.steps:
                state = run.steps[step.id]
                policy = step.retry
                if state.status != "waiting_external" or policy is None or policy.external_timeout_seconds is None:
                    continue
                if not state.external_submitted_at:
                    continue
                deadline = datetime.fromisoformat(state.external_submitted_at) + timedelta(seconds=policy.external_timeout_seconds)
                if now >= deadline:
                    self._needs_reconciliation(state, "External job timed out; remote execution may still be running")
                    changed = True
            if changed:
                self._save(run)
            return self.store.get(run_id)

    def reconcile_step(self, run_id: str, step_id: str, outcome: ReconciliationOutcome, *, actor: str, evidence: str, output: dict | None = None) -> WorkflowRun:
        """由可信宿主记录核实证据，并按真实远端结果完成、重排、失败或继续等待。"""
        with self._lock:
            if not actor or not actor.strip() or not evidence or not evidence.strip():
                raise ValueError("Trusted actor and non-empty evidence are required")
            if outcome not in {"succeeded", "not_executed", "failed", "still_running"}:
                raise ValueError("Unknown reconciliation outcome")
            run = self.store.get(run_id)
            state = run.steps.get(step_id)
            if state is None or state.status != "recovery_required":
                raise ValueError("Step does not require reconciliation")
            step = next(item for item in run.definition.steps if item.id == step_id)
            if outcome == "succeeded":
                safe_output = json_snapshot(output)
                if not isinstance(safe_output, dict):
                    raise TypeError("Confirmed success requires an explicit JSON object result")
                state.status = "succeeded"
                state.output = safe_output
                state.error = None
            elif outcome == "not_executed":
                if state.attempts >= step.max_attempts:
                    raise ValueError("Maximum attempts reached; cannot resubmit the step")
                state.status = "pending"
                state.output = None
                state.error = None
                state.external_submitted_at = None
            elif outcome == "still_running":
                if not isinstance(state.output, dict) or not all(key in state.output for key in ("provider", "reference", "metadata")):
                    raise ValueError("Only a previously submitted external job can resume waiting")
                state.status = "waiting_external"
                state.external_submitted_at = self._approval_now().isoformat()
                state.error = None
            else:
                state.status = "failed"
                state.error = f"Confirmed remote failure: {evidence}"
            state.retry_at = None
            self._record_recovery(state, "reconciled_" + outcome, actor, evidence)
            self._save(run)
            return self.store.get(run_id)

    def advance_one(self, run_id: str) -> bool:
        """持久化执行一次状态转移，最多执行一个动作或打开一个人工审批关卡。"""
        with self._lock:
            run = self.store.get(run_id)
            if run.status == "retry_wait":
                if not self._wake_due_retries(run):
                    return False
                run = self.store.get(run_id)
            if run.status != "running":
                return False
            candidate = self._candidate(run)
            if candidate is None:
                return False
            step, behavior = candidate
            state = run.steps[step.id]
            if behavior == "skip" or not self._matches_condition(run, step):
                state.status = "skipped"
                state.note = "Dependency was skipped or condition did not match"
                self._save(run)
                return True
            if step.kind == "approval":
                self._open_approval(run, step)
                return True
            state.status = "running"
            state.attempts += 1
            state.attempt_id = uuid4().hex
            state.error = None
            state.retry_at = None
            self._save(run)
            try:
                context = StepContext(
                    run_id=run.id, workflow_id=run.definition.id, step_id=step.id,
                    attempt=state.attempts, inputs=json_snapshot(run.inputs),
                    params=json_snapshot(step.params), results=json_snapshot({
                        key: value.output for key, value in run.steps.items() if value.status == "succeeded"
                    }),
                    attempt_id=state.attempt_id, idempotency_key=f"{run.id}:{step.id}",
                )
                output = self.actions.get(step.action)(context)
                if isinstance(output, ExternalSubmission):
                    state.output = {"provider": output.provider, "reference": output.reference, "metadata": json_snapshot(output.metadata)}
                    state.status = "waiting_external"
                    state.external_submitted_at = self._approval_now().isoformat()
                else:
                    safe_output = json_snapshot(output)
                    if not isinstance(safe_output, dict):
                        raise TypeError("Workflow action must return a JSON object")
                    state.output = safe_output
                    state.status = "succeeded"
            except UncertainActionError as error:
                self._needs_reconciliation(state, f"UncertainActionError: {error}")
            except RetryableActionError as error:
                if not self._schedule_safe_retry(state, step, error):
                    state.status = "failed"
                    state.error = f"RetryableActionError: {error}"
            except Exception as error:
                state.status = "failed"
                state.error = f"{type(error).__name__}: {error}"
            self._save(run)
            return True

    def run_until_blocked(self, run_id: str, max_transitions: int = 100) -> WorkflowRun:
        """按依赖顺序持续推进步骤，直到完成、失败、等待审批或达到本轮步数上限。"""
        if type(max_transitions) is not int or max_transitions < 1:
            raise ValueError("max_transitions must be positive")
        for _ in range(max_transitions):
            if not self.advance_one(run_id):
                break
        return self.store.get(run_id)

    def poll_external(self, run_id: str, step_id: str) -> WorkflowRun:
        """宿主轮询正在等待的外部步骤，只有查询到明确成功或失败后才推进工作流。"""
        with self._lock:
            run = self.store.get(run_id)
            state = run.steps.get(step_id)
            if state is None or state.status != "waiting_external" or not isinstance(state.output, dict):
                raise ValueError("Step is not waiting for an external job")
            ticket = state.output
            outcome = self.external.resolve(ticket["provider"], ticket["reference"], ticket["metadata"])
            if outcome.status == "running":
                return self.check_external_timeouts(run_id)
            if outcome.status == "unknown":
                state.note = "External result unknown; inspect remote state before any retry"
                self._save(run)
                return self.check_external_timeouts(run_id)
            if outcome.status == "succeeded":
                state.status = "succeeded"
                state.output = {"provider": ticket["provider"], "reference": ticket["reference"], "result": json_snapshot(outcome.output)}
            else:
                state.status = "failed"
                state.error = outcome.error or "External job failed"
            self._save(run)
            return self.store.get(run_id)

    def approve_step(self, run_id: str, step_id: str, approved: bool, note: str = "", *, actor: str | None = None, request_id: str | None = None) -> WorkflowRun:
        """由可信宿主核对审批人、请求编号和有效期，再原子记录批准或拒绝决定。"""
        with self._lock:
            run = self.store.get(run_id)
            step = next((item for item in run.definition.steps if item.id == step_id), None)
            if run.status != "waiting_approval" or step is None or step.kind != "approval":
                raise ValueError("Workflow is not waiting for this approval")
            state = run.steps[step_id]
            request = state.approval_request
            if state.status != "waiting_approval" or type(approved) is not bool:
                raise ValueError("Step is not waiting for approval or decision is not boolean")
            if request is None:
                if step.approval is not None:
                    raise ValueError("Protected approval request is missing")
                now_legacy = self._approval_now()
                request = ApprovalRequest(uuid4().hex, "waiting", now_legacy.isoformat())
                state.approval_request = request
                state.approval_events.append(ApprovalEvent(request.request_id, "requested", now_legacy.isoformat(), "legacy_host", "Migrated V7.1 approval request"))
            if type(note) is not str:
                raise TypeError("Approval note must be a string")
            if step.approval is not None:
                if not actor or not request_id:
                    raise PermissionError("Protected approval requires verified actor and request ID")
                if request_id != request.request_id:
                    raise ValueError("Stale or incorrect approval request ID")
                if actor not in step.approval.approvers:
                    raise PermissionError("Actor is not an allowed approver")
                if not step.approval.allow_self_approval and actor == step.approval.requester:
                    raise PermissionError("Requester cannot approve their own request")
                if not approved and step.approval.require_rejection_reason and not note.strip():
                    raise ValueError("Rejection reason is required")
            elif request_id is not None and request_id != request.request_id:
                raise ValueError("Stale or incorrect approval request ID")
            now = self._approval_now()
            if self._expire_approval(run, step_id, now):
                raise TimeoutError("Approval expired; request a new approval")
            effective_actor = actor or "legacy_host"
            request.status = "approved" if approved else "rejected"
            request.decided_at = now.isoformat()
            request.decided_by = effective_actor
            request.note = note
            state.status = "succeeded" if approved else "rejected"
            state.output = {"approved": approved, "note": note}
            state.note = note
            state.approval_events.append(ApprovalEvent(request.request_id, request.status, now.isoformat(), effective_actor, note))
            self._save(run)
            return self.store.get(run_id)

    def retry_failed_step(self, run_id: str, step_id: str, *, confirmed_safe: bool = False, actor: str = "", evidence: str = "") -> WorkflowRun:
        """只有宿主明确确认 V7.4 步骤安全可重试后才重排失败步骤，旧版步骤保持兼容。"""
        with self._lock:
            run = self.store.get(run_id)
            if run.status != "failed":
                raise ValueError("Workflow is not failed")
            step = next((item for item in run.definition.steps if item.id == step_id), None)
            if step is None or run.steps[step_id].status != "failed":
                raise ValueError("Step is not failed")
            state = run.steps[step_id]
            if state.attempts >= step.max_attempts:
                raise ValueError("Maximum attempts reached")
            if step.retry is not None:
                if not confirmed_safe or not actor.strip() or not evidence.strip():
                    raise PermissionError("Protected retry requires trusted confirmation, actor and evidence")
                self._record_recovery(state, "manual_retry_confirmed", actor, evidence)
            state.status = "pending"
            state.error = None
            state.output = None
            state.external_submitted_at = None
            state.retry_at = None
            self._save(run)
            return self.store.get(run_id)

    def recover_interrupted(self, run_id: str) -> WorkflowRun:
        """进程重启后将遗留的 running 步骤标成失败，不擅自重放可能已经产生副作用的动作。"""
        with self._lock:
            run = self.store.get(run_id)
            if run.status in {"completed", "rejected"}:
                return run
            changed = False
            for step_id, state in run.steps.items():
                if state.status == "running":
                    step = next(item for item in run.definition.steps if item.id == step_id)
                    if step.retry is not None:
                        self._needs_reconciliation(state, "Interrupted execution: remote side effect may have completed")
                    else:
                        state.status = "failed"
                        state.error = "Interrupted execution: remote side effect may have completed"
                    changed = True
            if changed:
                self._save(run)
            return self.store.get(run_id)
