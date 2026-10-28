"""验证 V7.3 审批授权、持久化审计、过期重发与 V7.2 外部等待不冲突。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from devpilot.workflow import (
    ActionRegistry, ApprovalPolicy, ExternalOutcome, ExternalResolverRegistry,
    ExternalSubmission, StepDefinition, WorkflowDefinition, WorkflowEngine, WorkflowStore,
)


class FakeClock:
    """提供可以手动推进的时钟，让审批到期测试不依赖 sleep。"""

    def __init__(self):
        """从固定 UTC 时间开始模拟审批时钟。"""
        self.current = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

    def __call__(self):
        """返回当前测试时间。"""
        return self.current

    def pass_seconds(self, seconds):
        """让模拟时钟向前推进指定秒数。"""
        self.current += timedelta(seconds=seconds)


def setup(tmp_path, *, policy=None, clock=None, external=None):
    """创建带审计存储的真实 Workflow Engine 和一个受控审批节点。"""
    actions = ActionRegistry()
    actions.register("prepare", lambda ctx: {"ready": True})
    calls = []
    actions.register("publish", lambda ctx: calls.append("published") or {"ok": True})
    engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external, clock=clock)
    flow = WorkflowDefinition("approve-demo", "审批后发布", (
        StepDefinition("prepare", action="prepare"),
        StepDefinition("review", kind="approval", depends_on=("prepare",), approval=policy),
        StepDefinition("publish", action="publish", depends_on=("review",)),
    ))
    run = engine.start(flow)
    assert engine.run_until_blocked(run.id).status == "waiting_approval"
    return engine, run.id, calls


def approve(engine, run_id, request_id, **kwargs):
    """模拟宿主用实际请求编号提交审核决定。"""
    return engine.approve_step(run_id, "review", True, actor="reviewer", request_id=request_id, **kwargs)


def test_protected_approval_unblocks_downstream(tmp_path):
    """只有授权审批人提交当前申请编号后才能执行下游发布步骤。"""
    engine, run_id, calls = setup(tmp_path, policy=ApprovalPolicy(("reviewer",), purpose="发布代码"))
    request = engine.get_approval_request(run_id, "review")
    assert len(request.request_id) == 32
    assert request.status == "waiting"
    assert calls == []
    result = approve(engine, run_id, request.request_id, note="已审查")
    assert result.steps["review"].approval_request.decided_by == "reviewer"
    assert result.steps["review"].approval_request.note == "已审查"
    assert [event.event for event in result.steps["review"].approval_events] == ["requested", "approved"]
    assert engine.run_until_blocked(run_id).status == "completed"
    assert calls == ["published"]


def test_old_api_cannot_bypass_protected_policy(tmp_path):
    """为审批节点设置白名单后，V7.1 的旧 approve_step 参数不足以绕过授权。"""
    engine, run_id, calls = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    with pytest.raises(PermissionError):
        engine.approve_step(run_id, "review", True)
    assert engine.store.get(run_id).status == "waiting_approval"
    assert calls == []


def test_unauthorized_actor_and_missing_request_id_are_denied(tmp_path):
    """未经授权的身份或缺少审批编号时，审批不能改变持久化状态。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    request_id = engine.get_approval_request(run_id, "review").request_id
    with pytest.raises(PermissionError):
        engine.approve_step(run_id, "review", True, actor="stranger", request_id=request_id)
    with pytest.raises(PermissionError):
        engine.approve_step(run_id, "review", True, actor="reviewer")
    assert len(engine.store.get(run_id).steps["review"].approval_events) == 1


def test_stale_request_id_is_rejected(tmp_path):
    """其他请求的编号不能用于当前待审批的 Workflow。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    with pytest.raises(ValueError, match="Stale"):
        approve(engine, run_id, "f" * 32)


def test_requester_cannot_approve_self(tmp_path):
    """审批策略禁止申请人本人用相同身份自我授权。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("alice", "reviewer"), requester="alice"))
    request_id = engine.get_approval_request(run_id, "review").request_id
    with pytest.raises(PermissionError, match="Requester"):
        engine.approve_step(run_id, "review", True, actor="alice", request_id=request_id)
    assert approve(engine, run_id, request_id).status == "running"


def test_explicit_self_approval_can_be_enabled_by_host(tmp_path):
    """只有宿主显式配置允许自批时申请人才能批准自己的流程。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("alice",), requester="alice", allow_self_approval=True))
    request_id = engine.get_approval_request(run_id, "review").request_id
    assert engine.approve_step(run_id, "review", True, actor="alice", request_id=request_id).status == "running"


def test_rejection_requires_reason_and_stops_downstream(tmp_path):
    """拒绝必须记录原因且立即终止工作流，绝不能触发后续动作。"""
    engine, run_id, calls = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    request_id = engine.get_approval_request(run_id, "review").request_id
    with pytest.raises(ValueError, match="reason"):
        engine.approve_step(run_id, "review", False, actor="reviewer", request_id=request_id)
    rejected = engine.approve_step(run_id, "review", False, "代码不符合规范", actor="reviewer", request_id=request_id)
    assert rejected.status == "rejected"
    assert rejected.steps["review"].approval_request.decided_by == "reviewer"
    assert rejected.steps["publish"].status == "pending"
    assert engine.run_until_blocked(run_id).status == "rejected"
    assert calls == []


def test_expiry_scanner_blocks_execution(tmp_path):
    """审批超过截止时间后，扫描器会持久化过期事件并阻止后续步骤。"""
    clock = FakeClock()
    engine, run_id, calls = setup(tmp_path, policy=ApprovalPolicy(("reviewer",), timeout_seconds=30), clock=clock)
    request_id = engine.get_approval_request(run_id, "review").request_id
    clock.pass_seconds(30)
    run = engine.expire_approvals(run_id)
    assert run.status == "approval_expired"
    assert run.steps["review"].approval_request.status == "expired"
    assert [event.event for event in run.steps["review"].approval_events] == ["requested", "expired"]
    with pytest.raises(ValueError):
        approve(engine, run_id, request_id)
    assert engine.run_until_blocked(run_id).steps["publish"].status == "pending"
    assert calls == []


def test_late_approval_expires_even_without_scan(tmp_path):
    """即使宿主忘记主动扫描，审批时也会再次检查期限而拒绝过期请求。"""
    clock = FakeClock()
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",), timeout_seconds=2), clock=clock)
    request_id = engine.get_approval_request(run_id, "review").request_id
    clock.pass_seconds(5)
    with pytest.raises(TimeoutError):
        approve(engine, run_id, request_id)
    assert WorkflowStore(tmp_path / "runs").get(run_id).status == "approval_expired"


def test_reissue_requires_fresh_id_and_retains_audit(tmp_path):
    """过期后只能显式重新发单，旧审批编号失效且历史记录永久保留。"""
    clock = FakeClock()
    engine, run_id, calls = setup(tmp_path, policy=ApprovalPolicy(("reviewer",), timeout_seconds=5), clock=clock)
    old_id = engine.get_approval_request(run_id, "review").request_id
    clock.pass_seconds(10)
    engine.expire_approvals(run_id)
    reopened = engine.reissue_approval(run_id, "review")
    fresh_id = reopened.steps["review"].approval_request.request_id
    assert fresh_id != old_id
    assert reopened.status == "waiting_approval"
    with pytest.raises(ValueError, match="Stale"):
        approve(engine, run_id, old_id)
    assert approve(engine, run_id, fresh_id).status == "running"
    assert engine.run_until_blocked(run_id).status == "completed"
    assert calls == ["published"]
    stored = WorkflowStore(tmp_path / "runs").get(run_id)
    assert [e.event for e in stored.steps["review"].approval_events] == ["requested", "expired", "requested", "approved"]


def test_reissue_rejected_request_is_forbidden(tmp_path):
    """被明确拒绝的请求不能伪装成过期申请重新发起。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    request_id = engine.get_approval_request(run_id, "review").request_id
    engine.approve_step(run_id, "review", False, "不通过", actor="reviewer", request_id=request_id)
    with pytest.raises(ValueError):
        engine.reissue_approval(run_id, "review")


def test_reopen_store_keeps_policy_request_and_audit(tmp_path):
    """重建 Engine 后依旧能读取白名单、待处理申请与全部审计事件。"""
    clock = FakeClock()
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",), timeout_seconds=60, purpose="审核上线"), clock=clock)
    request_id = engine.get_approval_request(run_id, "review").request_id
    new_engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), ActionRegistry(), clock=clock)
    loaded = new_engine.store.get(run_id)
    assert loaded.definition.steps[1].approval.approvers == ("reviewer",)
    assert loaded.definition.steps[1].approval.purpose == "审核上线"
    assert new_engine.get_approval_request(run_id, "review").request_id == request_id
    assert new_engine.approve_step(run_id, "review", True, actor="reviewer", request_id=request_id).status == "running"
    assert WorkflowStore(tmp_path / "runs").get(run_id).steps["review"].approval_events[-1].actor == "reviewer"


def test_duplicate_decision_does_not_create_duplicate_audit(tmp_path):
    """相同审批请求不能被重复批准，也不会产生重复审计事件。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    request_id = engine.get_approval_request(run_id, "review").request_id
    approve(engine, run_id, request_id)
    with pytest.raises(ValueError):
        approve(engine, run_id, request_id)
    assert len(engine.store.get(run_id).steps["review"].approval_events) == 2


def test_policy_validation(tmp_path):
    """拒绝空白审批人、重复身份、非法超时时间及附着在动作上的审批策略。"""
    with pytest.raises(ValueError):
        ApprovalPolicy(())
    with pytest.raises(ValueError):
        ApprovalPolicy(("reviewer", "reviewer"))
    with pytest.raises(ValueError):
        ApprovalPolicy(("reviewer",), timeout_seconds=0)
    with pytest.raises(ValueError):
        ApprovalPolicy(("reviewer",), timeout_seconds=True)
    engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), ActionRegistry())
    flow = WorkflowDefinition("invalid", "invalid", (StepDefinition("action", action="do", approval=ApprovalPolicy(("reviewer",))),))
    with pytest.raises(ValueError):
        engine.start(flow)


def test_v7_1_legacy_runs_without_new_fields_still_load(tmp_path):
    """旧 V7.1 JSON 缺少审批字段时依然能反序列化，旧接口可以继续使用。"""
    engine, run_id, _ = setup(tmp_path)
    path = tmp_path / "runs" / f"{run_id}.json"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    for spec in data["definition"]["steps"]:
        spec.pop("approval", None)
    for state in data["steps"].values():
        state.pop("approval_request", None)
        state.pop("approval_events", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = engine.store.get(run_id)
    assert loaded.definition.steps[1].approval is None
    assert engine.approve_step(run_id, "review", True, "旧版批准").status == "running"


def test_wrong_step_cannot_be_approved(tmp_path):
    """审批人不能使用其他动作节点的 ID 来绕开审批步骤。"""
    engine, run_id, _ = setup(tmp_path, policy=ApprovalPolicy(("reviewer",)))
    with pytest.raises(ValueError):
        engine.approve_step(run_id, "publish", True, actor="reviewer", request_id="x")


def test_v7_2_external_wait_keeps_approval_separate(tmp_path):
    """V7.2 等待外部 Job 完成后才打开审批单，审批通过后才能执行下游动作。"""
    actions = ActionRegistry()
    actions.register("submit", lambda ctx: ExternalSubmission("v5", "job-1"))
    seen = []
    actions.register("finish", lambda ctx: seen.append(True) or {"ok": True})
    external = ExternalResolverRegistry()
    statuses = ["running", "succeeded"]
    external.register("v5", lambda reference, metadata: ExternalOutcome(statuses.pop(0), {"tests": "passed"}))
    engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external)
    flow = WorkflowDefinition("external", "外部测试审批", (
        StepDefinition("test", action="submit"),
        StepDefinition("review", kind="approval", depends_on=("test",), approval=ApprovalPolicy(("reviewer",))),
        StepDefinition("finish", action="finish", depends_on=("review",)),
    ))
    run = engine.start(flow)
    assert engine.run_until_blocked(run.id).status == "waiting_external"
    assert engine.poll_external(run.id, "test").status == "waiting_external"
    assert engine.poll_external(run.id, "test").status == "running"
    assert engine.run_until_blocked(run.id).status == "waiting_approval"
    assert not seen
    request_id = engine.get_approval_request(run.id, "review").request_id
    approve(engine, run.id, request_id)
    assert engine.run_until_blocked(run.id).status == "completed"
    assert seen == [True]
