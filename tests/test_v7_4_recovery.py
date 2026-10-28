"""V7.4 故障注入测试：验证安全重试、未知结果人工核实、外部超时和审计持久化。"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from devpilot.workflow import (
    ActionRegistry, ExternalOutcome, ExternalResolverRegistry, ExternalSubmission,
    RecoveryEvent, RetryPolicy, RetryableActionError, StepDefinition, UncertainActionError,
    WorkflowDefinition, WorkflowEngine, WorkflowStore, validate_definition,
)


class FakeClock:
    """让重试和外部超时测试只移动模拟时间，不依赖真实 sleep。"""

    def __init__(self):
        """初始化一个固定的 UTC 测试时间。"""
        self.now = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        """把模拟的当前时间交给 WorkflowEngine。"""
        return self.now

    def advance(self, seconds):
        """根据用例要求推进模拟时间。"""
        self.now += timedelta(seconds=seconds)


def make_runtime(tmp_path, action, *, retry=None, max_attempts=3, extra=(), resolver=None, clock=None):
    """创建真实磁盘 Store 和含有一个可重试步骤的 WorkflowEngine。"""
    actions = ActionRegistry()
    actions.register("execute", action)
    for name, handler in extra:
        actions.register(name, handler)
    external = ExternalResolverRegistry()
    if resolver is not None:
        external.register("v5", resolver)
    engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external, clock=clock)
    flow = WorkflowDefinition("recovery", "恢复测试", (
        StepDefinition("work", action="execute", retry=retry, max_attempts=max_attempts),
        StepDefinition("final", action="finalize", depends_on=("work",)) if any(k == "finalize" for k, _ in extra) else StepDefinition("post", kind="approval", depends_on=("work",)),
    ))
    run = engine.start(flow)
    return engine, run.id, actions, external


def test_automatic_retry_requires_explicit_idempotence():
    """没有声明幂等性的动作绝不能启用自动重试。"""
    with pytest.raises(ValueError, match="idempotent"):
        RetryPolicy(automatic_on_transient=True)
    with pytest.raises(ValueError):
        RetryPolicy(delay_seconds=-1)
    with pytest.raises(ValueError):
        RetryPolicy(external_timeout_seconds=0)
    with pytest.raises(ValueError):
        RetryPolicy(delay_seconds=float("inf"))
    with pytest.raises(FrozenInstanceError):
        RetryPolicy().idempotent = True


def test_retry_policy_only_on_action_steps():
    """审批节点不允许配置自动重试，防止自动越过人工关卡。"""
    definition = WorkflowDefinition("x", "x", (StepDefinition("approve", kind="approval", retry=RetryPolicy()),))
    with pytest.raises(ValueError, match="Only action"):
        validate_definition(definition)


def test_retryable_error_waits_then_retries_with_same_idempotency_key(tmp_path):
    """明确幂等的临时错误进入等待，时间到后自动重试且保持统一幂等键。"""
    clock = FakeClock()
    contexts = []

    def flaky(ctx):
        """首次抛出明确可重试异常，第二次返回成功。"""
        contexts.append(ctx)
        if len(contexts) == 1:
            raise RetryableActionError("temporary")
        return {"done": True}

    engine, run_id, _, _ = make_runtime(tmp_path, flaky, retry=RetryPolicy(True, True, delay_seconds=5), clock=clock)
    first = engine.run_until_blocked(run_id)
    assert first.status == "retry_wait"
    assert first.steps["work"].attempts == 1
    assert engine.run_until_blocked(run_id).status == "retry_wait"
    assert len(contexts) == 1
    clock.advance(5)
    next_run = engine.run_until_blocked(run_id)
    assert next_run.status == "waiting_approval"
    assert next_run.steps["work"].attempts == 2
    assert contexts[0].attempt_id != contexts[1].attempt_id
    assert contexts[0].idempotency_key == contexts[1].idempotency_key == f"{run_id}:work"
    events = [e.event for e in next_run.steps["work"].recovery_events]
    assert events == ["retry_scheduled", "retry_due"]


def test_zero_delay_automatic_retry_immediately_succeeds(tmp_path):
    """重试延迟为零时同一轮调度可以继续，但仍受最大尝试次数约束。"""
    calls = []

    def flaky(ctx):
        """第一次失败，第二次返回成功结果。"""
        calls.append(ctx.attempt)
        if len(calls) == 1:
            raise RetryableActionError("temporary")
        return {"ok": True}

    engine, run_id, _, _ = make_runtime(tmp_path, flaky, retry=RetryPolicy(True, True))
    assert engine.run_until_blocked(run_id).status == "waiting_approval"
    assert calls == [1, 2]


def test_automatic_retry_never_runs_past_attempt_limit(tmp_path):
    """达到尝试上限后自动重试立即停止并记录失败。"""
    calls = []

    def fail(ctx):
        """每次执行都报告明确的临时错误。"""
        calls.append(ctx.attempt)
        raise RetryableActionError("still failing")

    engine, run_id, _, _ = make_runtime(tmp_path, fail, retry=RetryPolicy(True, True), max_attempts=2)
    assert engine.run_until_blocked(run_id).status == "failed"
    assert calls == [1, 2]
    assert engine.run_until_blocked(run_id).status == "failed"


def test_generic_errors_are_never_auto_retried(tmp_path):
    """即使开启自动重试，普通异常也不能被擅自认作可重试的临时错误。"""
    calls = []

    def error(ctx):
        """抛出没有可靠重试语义的一般错误。"""
        calls.append(True)
        raise ConnectionError("ambiguous side effect")

    engine, run_id, _, _ = make_runtime(tmp_path, error, retry=RetryPolicy(True, True))
    assert engine.run_until_blocked(run_id).status == "failed"
    assert len(calls) == 1


def test_uncertain_result_requires_reconciliation_and_blocks_downstream(tmp_path):
    """调用后结果未知时暂停整条工作流，不能自动重复产生副作用。"""
    calls = []
    downstream = []

    def uncertain(ctx):
        """模拟远端可能成功但本地没有收到结果。"""
        calls.append(True)
        raise UncertainActionError("response lost")

    engine, run_id, _, _ = make_runtime(tmp_path, uncertain, retry=RetryPolicy(True, True), extra=[("finalize", lambda ctx: downstream.append(1) or {"done": True})])
    run = engine.run_until_blocked(run_id)
    assert run.status == "recovery_required"
    assert run.steps["final"].status == "pending"
    assert len(calls) == 1
    assert engine.run_until_blocked(run_id).status == "recovery_required"
    assert downstream == []
    with pytest.raises(ValueError):
        engine.retry_failed_step(run_id, "work")


def test_reconcile_success_allows_downstream_and_has_audit(tmp_path):
    """宿主核实远端已经成功后可以直接恢复下游，不再次执行原动作。"""
    calls = []
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: calls.append(1) or (_ for _ in ()).throw(UncertainActionError("no response")), retry=RetryPolicy())
    engine.run_until_blocked(run_id)
    with pytest.raises(ValueError, match="evidence"):
        engine.reconcile_step(run_id, "work", "succeeded", actor="ops", evidence="")
    with pytest.raises(TypeError):
        engine.reconcile_step(run_id, "work", "succeeded", actor="ops", evidence="ticket says done")
    recovered = engine.reconcile_step(run_id, "work", "succeeded", actor="ops", evidence="Git commit abc123", output={"done": True})
    assert recovered.status == "running"
    assert len(calls) == 1
    assert engine.run_until_blocked(run_id).status == "waiting_approval"
    event = engine.store.get(run_id).steps["work"].recovery_events[-1]
    assert (event.event, event.actor, event.evidence) == ("reconciled_succeeded", "ops", "Git commit abc123")


def test_confirm_not_executed_can_retry_only_with_remaining_attempt(tmp_path):
    """确认动作根本没有发生时才重新排队，且原尝试次数必须保留。"""
    calls = []

    def uncertain(ctx):
        """记录调用次数并模拟一次结果未知的操作。"""
        calls.append(ctx.attempt)
        if len(calls) == 1:
            raise UncertainActionError("not confirmed")
        return {"ok": True}

    engine, run_id, _, _ = make_runtime(tmp_path, uncertain, retry=RetryPolicy(), max_attempts=2)
    assert engine.run_until_blocked(run_id).status == "recovery_required"
    assert engine.reconcile_step(run_id, "work", "not_executed", actor="operator", evidence="remote journal empty").status == "running"
    assert engine.run_until_blocked(run_id).status == "waiting_approval"
    assert calls == [1, 2]


def test_not_executed_does_not_bypass_attempt_limit(tmp_path):
    """即使证明未执行，也不能绕过明确设置的最大尝试次数。"""
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: (_ for _ in ()).throw(UncertainActionError("unknown")), retry=RetryPolicy(), max_attempts=1)
    engine.run_until_blocked(run_id)
    with pytest.raises(ValueError, match="Maximum attempts"):
        engine.reconcile_step(run_id, "work", "not_executed", actor="ops", evidence="no record")


def test_confirm_remote_failure_needs_explicit_manual_retry(tmp_path):
    """外部失败核实后保持 failed，受保护步骤的人工重试必须有安全证据。"""
    calls = []

    def action(ctx):
        """第一次模拟结果未知，第二次返回成功。"""
        calls.append(1)
        if len(calls) == 1:
            raise UncertainActionError("response lost")
        return {"ok": True}

    engine, run_id, _, _ = make_runtime(tmp_path, action, retry=RetryPolicy(), max_attempts=2)
    engine.run_until_blocked(run_id)
    engine.reconcile_step(run_id, "work", "failed", actor="ops", evidence="remote reports failure")
    with pytest.raises(PermissionError):
        engine.retry_failed_step(run_id, "work")
    engine.retry_failed_step(run_id, "work", confirmed_safe=True, actor="ops", evidence="remote confirmed no side effect")
    assert engine.run_until_blocked(run_id).status == "waiting_approval"
    assert calls == [1, 1]


def test_restart_running_protected_step_requires_reconciliation(tmp_path):
    """崩溃恢复时受保护步骤进入待核实状态，绝不自动重放。"""
    calls = []
    engine, run_id, actions, external = make_runtime(tmp_path, lambda ctx: calls.append(1) or {"ok": True}, retry=RetryPolicy(True, True), max_attempts=2)
    run = engine.store.get(run_id)
    state = run.steps["work"]
    state.status = "running"
    state.attempts = 1
    state.attempt_id = "previous-attempt"
    engine.store.save(run)
    restarted = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external)
    assert restarted.recover_interrupted(run_id).status == "recovery_required"
    assert restarted.recover_interrupted(run_id).steps["work"].attempts == 1
    assert len(restarted.store.get(run_id).steps["work"].recovery_events) == 1
    assert calls == []


def test_restart_waiting_external_does_not_resubmit(tmp_path):
    """重启遇到 waiting_external 时保留原 Job ID，只查询旧任务而不重复创建。"""
    jobs = []
    engine, run_id, actions, external = make_runtime(tmp_path, lambda ctx: jobs.append(1) or ExternalSubmission("v5", "job-42"), retry=RetryPolicy(external_timeout_seconds=300), resolver=lambda ref, meta: ExternalOutcome("succeeded", {"done": True}))
    assert engine.run_until_blocked(run_id).status == "waiting_external"
    restarted = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external)
    assert restarted.recover_interrupted(run_id).status == "waiting_external"
    assert restarted.poll_external(run_id, "work").status == "running"
    assert jobs == [1]


def test_external_wait_timeout_requires_reconciliation(tmp_path):
    """后台任务超时后仍保留凭据并阻塞下游，不能推断远端失败。"""
    clock = FakeClock()
    jobs = []
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: jobs.append(1) or ExternalSubmission("v5", "job-99"), retry=RetryPolicy(external_timeout_seconds=10), resolver=lambda ref, meta: ExternalOutcome("running"), clock=clock)
    assert engine.run_until_blocked(run_id).status == "waiting_external"
    clock.advance(9)
    assert engine.check_external_timeouts(run_id).status == "waiting_external"
    clock.advance(1)
    overdue = engine.check_external_timeouts(run_id)
    assert overdue.status == "recovery_required"
    assert overdue.steps["work"].output["reference"] == "job-99"
    assert jobs == [1]
    assert engine.run_until_blocked(run_id).status == "recovery_required"


def test_reconcile_still_running_restores_waiting_and_preserves_ticket(tmp_path):
    """宿主核实后台仍在运行后恢复轮询，继续使用原 Job ID。"""
    clock = FakeClock()
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: ExternalSubmission("v5", "job-42"), retry=RetryPolicy(external_timeout_seconds=3), resolver=lambda ref, meta: ExternalOutcome("running"), clock=clock)
    engine.run_until_blocked(run_id)
    clock.advance(5)
    engine.check_external_timeouts(run_id)
    resumed = engine.reconcile_step(run_id, "work", "still_running", actor="ops", evidence="V5 JobStore status=running")
    assert resumed.status == "waiting_external"
    assert resumed.steps["work"].output["reference"] == "job-42"
    assert engine.poll_external(run_id, "work").status == "waiting_external"


def test_still_running_requires_actual_external_ticket(tmp_path):
    """普通动作不可以假装有外部 Job 并跳回等待状态。"""
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: (_ for _ in ()).throw(UncertainActionError("unknown")), retry=RetryPolicy())
    engine.run_until_blocked(run_id)
    with pytest.raises(ValueError, match="submitted external"):
        engine.reconcile_step(run_id, "work", "still_running", actor="ops", evidence="no ticket")


def test_success_from_external_after_deadline_still_counts(tmp_path):
    """如果远端明确返回成功，即使已经过了等待期限，也使用真实成功结果。"""
    clock = FakeClock()
    engine, run_id, _, _ = make_runtime(tmp_path, lambda ctx: ExternalSubmission("v5", "job-42"), retry=RetryPolicy(external_timeout_seconds=2), resolver=lambda ref, meta: ExternalOutcome("succeeded", {"passed": True}), clock=clock)
    engine.run_until_blocked(run_id)
    clock.advance(10)
    assert engine.poll_external(run_id, "work").status == "running"


def test_recovery_and_retry_state_survive_store_reopen(tmp_path):
    """重试期限、尝试编号与审计记录能完整保存并在重启后还原。"""
    clock = FakeClock()
    engine, run_id, actions, ext = make_runtime(tmp_path, lambda ctx: (_ for _ in ()).throw(RetryableActionError("busy")), retry=RetryPolicy(True, True, delay_seconds=12), clock=clock)
    engine.run_until_blocked(run_id)
    old = engine.store.get(run_id)
    reopened = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, ext, clock=clock)
    saved = reopened.store.get(run_id)
    assert saved.definition.steps[0].retry == RetryPolicy(True, True, delay_seconds=12)
    assert saved.steps["work"].retry_at == old.steps["work"].retry_at
    assert isinstance(saved.steps["work"].recovery_events[0], RecoveryEvent)
    assert saved.steps["work"].attempt_id
    clock.advance(12)
    assert reopened.run_until_blocked(run_id).status == "retry_wait"
    assert reopened.store.get(run_id).steps["work"].attempts == 2
