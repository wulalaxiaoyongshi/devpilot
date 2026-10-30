"""验证 V8.6 会话推进、Git 与证据完整性、审批、安全恢复和交付报告。"""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from devpilot.goal import (
    AcceptanceCriterion, GoalApplication, GoalBudget, GoalDeliveryBuilder,
    GoalReportStore, GoalSession, GoalSpec,
)
from devpilot.workflow import (
    ApprovalPolicy, StepDefinition, WorkflowApplication, WorkflowDefinition,
)
from examples.v8_6_goal_driven_coding_demo import demo_bindings, git, make_repo, run_demo


@pytest.fixture
def stack(tmp_path):
    """构建可真正提交 Git 且允许断点重启的 V7 + V8 小型系统。"""
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    with WorkflowApplication(state / "workflows", demo_bindings()) as workflow:
        def fix(ctx):
            """将登录错误修复后真正创建 Git Commit。"""
            (repo / "auth.py").write_text("def login(password):\n    return 200 if password == 'secret' else 401  # fixed\n", encoding="utf-8")
            git(repo, "add", "auth.py")
            git(repo, "-c", "user.name=DevPilot", "-c", "user.email=devpilot@example.invalid", "commit", "-qm", "fix-login")
            return {"commit": git(repo, "rev-parse", "HEAD")}

        def noop(ctx):
            """模拟成功执行却没有修改代码的修复步骤。"""
            return {"noop": True}

        workflow.actions.register("test.fix", fix)
        workflow.actions.register("test.noop", noop)

        def plan(feedback):
            """根据可信测试选项生成预定义修复工作流，绝不执行反馈文字。"""
            if feedback.goal_title == "无审批计划":
                return WorkflowDefinition("simple", "无审批", (StepDefinition("fix", action="test.fix"),))
            if feedback.goal_title == "没有修复":
                return WorkflowDefinition("noop", "无修改", (StepDefinition("noop", action="test.noop"),))
            return WorkflowDefinition("reviewed", "需人工审批", (
                StepDefinition("fix", action="test.fix"),
                StepDefinition("review", kind="approval", depends_on=("fix",),
                               approval=ApprovalPolicy(("reviewer",), requester="agent", purpose="检查代码")),
            ))

        app = GoalApplication(state, repo, workflow, plan)
        yield app, repo, workflow, state


def start_goal(app, *, title="需要人工审批", path="fixed.py", policy=None):
    """创建一个需要新文件存在的 Goal，方便测试修复与预算分支。"""
    spec = GoalSpec("fix", title, "修复代码", (
        AcceptanceCriterion("file", "登录代码满足验收", "file_contains",
                            {"path": "auth.py", "contains": "else 401"}) if path == "auth.py"
        else AcceptanceCriterion("file", "修复文件必须存在", "file_exists", {"path": path}),
    ))
    run = app.start(spec, policy or GoalBudget(max_rounds=2))
    app.begin_execution(run.id)
    app.begin_verification(run.id)
    return run.id


def approve(workflow, update):
    """通过真实审批 API 传入被允许的身份与审批请求编号。"""
    item = workflow.pending_approvals(update.workflow_run_id)[0]
    workflow.approve(item.run_id, item.step_id, actor="reviewer", request_id=item.request_id,
                     approved=True, note="测试已检查")


def test_full_git_and_unittest_demo(tmp_path):
    """完整业务从真实回归失败、审批、Git 修复到证据报告和预算耗尽全部跑通。"""
    result = run_demo(tmp_path)
    assert result["first_boundary"] == "waiting_approval"
    assert result["delivery"] == "ready_for_delivery"
    assert result["final_goal"] == "verified"
    assert result["dirty_worktree_delivery"] == "verification_stale"
    assert result["budget_goal"] == "exhausted"
    assert result["repair_rounds"] == 1
    assert result["evidence_count"] == 6
    assert Path(result["markdown_report"]).is_file()
    assert "auth.py" in Path(result["markdown_report"]).read_text(encoding="utf-8")
    assert "returncode" in Path(result["markdown_report"]).read_text(encoding="utf-8")


def test_verified_report_roundtrip_and_reopen(tmp_path):
    """新的 ReportStore 能重新读取真实 Demo 产生的全部审计快照。"""
    result = run_demo(tmp_path)
    report_path = Path(result["json_report"])
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    report = GoalReportStore(tmp_path / "state" / "reports").get(raw["goal_run_id"], raw["id"])
    assert report.delivery_status == "ready_for_delivery"
    assert report.reviewer == "reviewer"
    assert len(report.repairs) == 1
    assert report.repairs[0].commits
    assert "auth.py" in report.changed_files
    assert {item.criterion_id for item in report.criteria} == {"correct-code", "regression", "tests-exist"}
    assert next(item for item in report.criteria if item.criterion_id == "regression").observed["returncode"] == 0
    history = GoalReportStore(tmp_path / "state" / "reports").list_for_goal(report.goal_run_id)
    assert len(history) == 2
    assert {item.delivery_status for item in history} == {"ready_for_delivery", "verification_stale"}


def test_no_auto_approval_and_no_duplicate_dispatch(stack):
    """一旦进入等待人工审批，多次推进不会重新提交相同的修复工作流。"""
    app, _, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    session = GoalSession(app)
    first = session.run_until_boundary(gid)
    assert first[-1].action == "waiting_approval"
    before = len(workflow.store.list_runs())
    for _ in range(3):
        update = session.advance(gid)
        assert update.action == "waiting_approval"
    assert len(workflow.store.list_runs()) == before == 1
    assert app.get(gid).status == "repairing"
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "not_verified"
    approve(workflow, first[-1])
    last = session.run_until_boundary(gid)
    assert last[-1].goal_status == "verified"
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "ready_for_delivery"


def test_unapproved_completed_workflow_not_deliverable(stack):
    """即使 V8 验收通过，没有人工审批记录也不能声称已获准交付。"""
    app, _, _, _ = stack
    gid = start_goal(app, title="无审批计划", path="auth.py")
    steps = GoalSession(app).run_until_boundary(gid)
    assert steps[-1].goal_status == "verified"
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "awaiting_review"
    assert GoalDeliveryBuilder(app, require_approval=False).build(gid).delivery_status == "ready_for_delivery"


def test_untracked_change_blocks_delivery_after_verification(stack):
    """即使 Goal verified，之后出现未跟踪文件也会撤销当前交付资格。"""
    app, repo, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    s = GoalSession(app)
    approve(workflow, s.run_until_boundary(gid)[-1])
    assert s.run_until_boundary(gid)[-1].goal_status == "verified"
    (repo / "surprise.txt").write_text("dirty", encoding="utf-8")
    report = GoalDeliveryBuilder(app).build(gid)
    assert report.delivery_status == "verification_stale"
    assert any("未提交" in item for item in report.warnings)


def test_clean_new_commit_also_stales_delivery(stack):
    """验收结束后即使工作区干净，新 Git Commit 也不能继承旧证据。"""
    app, repo, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    s = GoalSession(app)
    approve(workflow, s.run_until_boundary(gid)[-1])
    assert s.run_until_boundary(gid)[-1].goal_status == "verified"
    (repo / "new.txt").write_text("next", encoding="utf-8")
    git(repo, "add", "new.txt")
    git(repo, "-c", "user.name=T", "-c", "user.email=test@example.invalid", "commit", "-qm", "next")
    report = GoalDeliveryBuilder(app).build(gid)
    assert report.git_clean is True
    assert report.delivery_status == "verification_stale"
    assert any("Git HEAD" in item for item in report.warnings)


def test_unknown_evidence_stops_without_repair(stack):
    """工作区有未跟踪文件时证据未知，不能被自动当成失败并执行修复。"""
    app, repo, workflow, _ = stack
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
    gid = start_goal(app, path="auth.py")
    updates = GoalSession(app).run_until_boundary(gid)
    assert updates[-1].action == "verification_unknown"
    assert app.get(gid).status == "unknown"
    assert not workflow.store.list_runs()


def test_required_manual_criterion_stays_unknown(stack):
    """没有可信人工证据的必要条件必须为 UNKNOWN，不能触发代码修复。"""
    app, _, workflow, _ = stack
    spec = GoalSpec("manual-goal", "人工复核", "要求人工证据", (
        AcceptanceCriterion("review", "人工检查", "manual", {"instructions": "确认接口行为"}),
    ))
    run = app.start(spec, GoalBudget())
    app.begin_execution(run.id)
    app.begin_verification(run.id)
    assert GoalSession(app).run_until_boundary(run.id)[-1].action == "verification_unknown"
    assert not workflow.store.list_runs()


def test_prepared_ticket_dispatches_once(stack):
    """准备阶段落盘后重启前的修复单仅被提交一次。"""
    app, _, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    s = GoalSession(app)
    assert s.advance(gid).action == "collected"
    assert s.advance(gid).action == "repair_prepared"
    assert len(app.repair_store.list_for_goal(gid)) == 1
    assert s.advance(gid).action == "repair_dispatched"
    assert s.run_until_boundary(gid)[-1].action == "waiting_approval"
    assert len(workflow.store.list_runs()) == 1


def test_dispatch_unknown_never_auto_replays(stack):
    """修复单已标记为派发中但没有启动响应时，多次执行都只会要求人工对账。"""
    app, _, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    s = GoalSession(app)
    s.advance(gid)
    prepared = app.prepare_repair(gid)
    app.repair_store.update(prepared, status="dispatching")
    for _ in range(2):
        assert s.advance(gid).action == "needs_reconciliation"
    assert len(workflow.store.list_runs()) == 0


def test_restart_while_waiting_approval(stack):
    """重新创建 GoalApplication 与 WorkflowApplication 后能继续原审批单而不重复执行动作。"""
    app, repo, workflow, state = stack
    gid = start_goal(app, path="auth.py")
    old = GoalSession(app).run_until_boundary(gid)[-1]
    assert old.action == "waiting_approval"
    previous_head = git(repo, "rev-parse", "HEAD")
    with WorkflowApplication(state / "workflows", demo_bindings()) as reopened_wf:
        reopened = GoalApplication(state, repo, reopened_wf, lambda feedback: None)
        sess = GoalSession(reopened)
        snap = sess.advance(gid)
        assert snap.action == "waiting_approval"
        assert len(reopened_wf.store.list_runs()) == 1
        assert git(repo, "rev-parse", "HEAD") == previous_head
        approve(reopened_wf, snap)
        assert sess.run_until_boundary(gid)[-1].goal_status == "verified"
        assert GoalDeliveryBuilder(reopened).build(gid).delivery_status == "ready_for_delivery"


def test_budget_exhausted_without_new_dispatch(stack):
    """第一轮没有修好问题时预算耗尽，绝不生成第二个 Workflow。"""
    app, _, workflow, _ = stack
    gid = start_goal(app, title="没有修复", path="never_created.py", policy=GoalBudget(max_rounds=1))
    updates = GoalSession(app).run_until_boundary(gid)
    assert updates[-1].goal_status == "exhausted"
    assert len(workflow.store.list_runs()) == 1
    assert len(app.repair_store.list_for_goal(gid)) == 1
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "not_verified"


def test_pending_goal_requires_real_execution(stack):
    """宿主尚未执行初始开发流程时 Session 不会自行宣布进入验证阶段。"""
    app, _, workflow, _ = stack
    spec = GoalSpec("pending", "等待开发", "尚未执行", (
        AcceptanceCriterion("file", "存在目标文件", "file_exists", {"path": "new.py"}),
    ))
    run = app.start(spec, GoalBudget())
    assert GoalSession(app).advance(run.id).action == "need_initial_workflow"
    assert not workflow.store.list_runs()


@pytest.mark.parametrize("limit", [0, -1, True, 1001, 1.5])
def test_session_limit_rejects_invalid_values(stack, limit):
    """推进次数必须是有限正整数，避免宿主无意中形成无限循环。"""
    app, _, _, _ = stack
    with pytest.raises(ValueError):
        GoalSession(app).run_until_boundary("0" * 32, max_steps=limit)


def test_report_store_refuses_rewrite_and_bad_ids(stack):
    """已创建的报告不可覆盖，外部输入不能用路径穿越读取任意文件。"""
    app, _, _, state = stack
    gid = start_goal(app, title="没有修复", path="new.py")
    report = GoalDeliveryBuilder(app).build(gid)
    store = GoalReportStore(state / "reports")
    a, b = store.append(report)
    assert a.is_file() and b.is_file()
    assert store.get(gid, report.id) == report
    with pytest.raises(FileExistsError):
        store.append(report)
    for name in ["../etc", "../../other", "not-an-id", ""]:
        with pytest.raises(ValueError):
            store.get(name, report.id)
        with pytest.raises(ValueError):
            store.get(gid, name)


def test_report_detects_own_id_tampering(stack):
    """JSON 报告里的目标编号若与文件位置不匹配必须拒绝读取。"""
    app, _, _, state = stack
    gid = start_goal(app, path="new.py")
    report = GoalDeliveryBuilder(app).build(gid)
    store = GoalReportStore(state / "reports")
    path, _ = store.append(report)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["goal_run_id"] = "f" * 32
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        store.get(gid, report.id)


def test_report_snapshot_is_not_fake_success(stack):
    """尚在 verifying 的 Goal 只能得到 not_verified 的报告。"""
    app, _, _, _ = stack
    gid = start_goal(app, path="missing.py")
    report = GoalDeliveryBuilder(app).build(gid)
    assert report.goal_status == "verifying"
    assert report.delivery_status == "not_verified"
    assert report.git_clean
    assert report.warnings


def test_markdown_escapes_untrusted_columns(stack):
    """用户的验收描述即使包含表格分隔符也不会破坏 Markdown 表格结构。"""
    app, _, _, _ = stack
    spec = GoalSpec("md", "包含 | 管道", "摘要", (
        AcceptanceCriterion("x", "检查 | 文本", "file_exists", {"path": "new.py"}),
    ))
    run = app.start(spec, GoalBudget())
    report = GoalDeliveryBuilder(app).build(run.id)
    markdown = GoalDeliveryBuilder.render_markdown(report)
    assert "检查 \\| 文本" in markdown
    assert "not_verified" in markdown


def test_report_builder_requires_bool_review_policy(stack):
    """报告的强制审批配置不允许用字符串代替布尔值。"""
    app, _, _, _ = stack
    with pytest.raises(TypeError):
        GoalDeliveryBuilder(app, require_approval="False")


def test_only_latest_repair_review_can_unlock_delivery(tmp_path):
    """第一轮人工批准不能替第二轮没有审批的代码修改签字。"""
    repo = make_repo(tmp_path)
    state = tmp_path / "state"
    with WorkflowApplication(state / "workflows", demo_bindings()) as wf:
        state_counter = {"plans": 0}

        def noop(ctx):
            """第一轮即使得到批准也故意不修复实际代码。"""
            return {"noop": True}

        def fix(ctx):
            """第二轮修复真实代码并创建 Commit，但不申请审批。"""
            (repo / "auth.py").write_text("def login(password):\n    return 200 if password == 'secret' else 401 #new\n", encoding="utf-8")
            git(repo, "add", "auth.py")
            git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "fix-second")
            return {"commit": git(repo, "rev-parse", "HEAD")}

        wf.actions.register("demo.first_noop", noop)
        wf.actions.register("demo.second_fix", fix)

        def trusted_plan(feedback):
            """依照预置的固定轮次生成两条不同工作流。"""
            state_counter["plans"] += 1
            if state_counter["plans"] == 1:
                return WorkflowDefinition("first", "有旧审批", (
                    StepDefinition("noop", action="demo.first_noop"),
                    StepDefinition("review", kind="approval", depends_on=("noop",),
                                   approval=ApprovalPolicy(("reviewer",), requester="agent")),
                ))
            return WorkflowDefinition("second", "无新审批", (StepDefinition("fix", action="demo.second_fix"),))

        app = GoalApplication(state, repo, wf, trusted_plan)
        gid = start_goal(app, path="auth.py", policy=GoalBudget(max_rounds=2))
        session = GoalSession(app)
        first = session.run_until_boundary(gid)[-1]
        assert first.action == "waiting_approval"
        approve(wf, first)
        later = session.run_until_boundary(gid)
        assert later[-1].goal_status == "verified", later
        report = GoalDeliveryBuilder(app).build(gid)
        assert len(report.repairs) == 2
        assert report.delivery_status == "awaiting_review"
        assert report.reviewer is None


def test_interrupted_running_workflow_cannot_be_replayed_automatically(stack):
    """崩溃遗留的 running 步骤没有活动线程时必须先核实，不能重新执行修复动作。"""
    app, repo, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    sess = GoalSession(app)
    sess.advance(gid)
    ticket = app.dispatch_repair(gid, app.prepare_repair(gid).id)
    run = workflow.store.get(ticket.workflow_run_id)
    run.steps["fix"].status = "running"
    run.steps["fix"].attempts = 1
    workflow.store.save(run)
    before = git(repo, "rev-parse", "HEAD")
    update = sess.advance(gid)
    assert update.action == "needs_recovery"
    assert git(repo, "rev-parse", "HEAD") == before
    assert len(workflow.store.list_runs()) == 1


def test_report_counts_idempotent_host_usage(stack):
    """交付报告只汇总宿主实际记账事件，重复事件不会增加 Token 与费用。"""
    app, _, _, _ = stack
    gid = start_goal(app, path="auth.py")
    app.record_usage(gid, "real-call:1", tokens=135, cost_usd=0.014, source="test-host")
    app.record_usage(gid, "real-call:1", tokens=135, cost_usd=0.014, source="test-host")
    report = GoalDeliveryBuilder(app).build(gid)
    assert report.used_tokens == 135
    assert report.used_cost_usd == 0.014


def test_report_detects_newer_evidence_after_old_verification(stack):
    """Goal 已通过之后，出现比原验收更新的证据时旧报告不再直接具备交付资格。"""
    app, repo, workflow, _ = stack
    gid = start_goal(app, path="auth.py")
    session = GoalSession(app)
    approve(workflow, session.run_until_boundary(gid)[-1])
    assert session.run_until_boundary(gid)[-1].goal_status == "verified"
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "ready_for_delivery"
    previous = app.evidence_store.list_for_goal(gid)[-1]
    from uuid import uuid4
    from datetime import datetime, timedelta, timezone
    new_time = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    newer = replace(previous, id=uuid4().hex, captured_at=new_time)
    app.evidence_store.append(newer)
    assert GoalDeliveryBuilder(app).build(gid).delivery_status == "verification_stale"


def test_demo_refuses_existing_repo(tmp_path):
    """演示程序拒绝覆盖用户已经存在的仓库，避免示例误删实际代码。"""
    make_repo(tmp_path)
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        run_demo(tmp_path)
