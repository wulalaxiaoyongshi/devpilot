"""检查 V8.5 预算用量、修复门禁、跨重启恢复及真实 V7 集成。"""

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devpilot.goal import (
    AcceptanceCriterion, BudgetConflictError, BudgetExceededError, BudgetStore,
    GoalApplication, GoalBudget, GoalBudgetManager, GoalSpec,
)
from devpilot.workflow import ActionRegistry, StepDefinition, WorkflowDefinition, WorkflowEngine, WorkflowStore


class FakeClock:
    """提供可精确调整的测试时间而不依赖实际等待。"""

    def __init__(self):
        """从固定的 UTC 日期开始计时。"""
        self.value = datetime(2026, 10, 29, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        """返回测试维护的当前时间。"""
        return self.value

    def advance(self, seconds):
        """按指定秒数推进或回退模拟时钟。"""
        self.value += timedelta(seconds=seconds)


def git(repo: Path, *args: str) -> str:
    """在测试临时仓库运行 Git 命令并直接报告错误。"""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def context(tmp_path):
    """创建真实 Git 仓库、V7 执行器和一套可重启的 V8.5 应用。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# testing\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.name=T", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    clock = FakeClock()
    actions = ActionRegistry()
    workflow = WorkflowEngine(WorkflowStore(tmp_path / "state" / "workflows"), actions)

    def fix(ctx):
        """根据可信动作模板生成实际文件和 Git Commit。"""
        (repo / "patched.py").write_text("def status():\n    return 401\n", encoding="utf-8")
        git(repo, "add", "patched.py")
        git(repo, "-c", "user.name=T", "-c", "user.email=test@example.invalid", "commit", "-qm", "repair")
        return {"commit": git(repo, "rev-parse", "HEAD")}

    def noop(ctx):
        """模拟正常结束但没有修复失败验收条件的 Worker。"""
        return {"noop": True}

    actions.register("test.fix", fix)
    actions.register("test.noop", noop)

    def trusted_plan(feedback):
        """只允许预先登记的修复动作，不执行反馈描述中的文字。"""
        return WorkflowDefinition("repair", "修复", (StepDefinition("fix", action="test.fix"),))

    app = GoalApplication(tmp_path / "state", repo, workflow, trusted_plan, clock=clock)
    return app, repo, workflow, clock, tmp_path / "state"


def goal(app, *, policy=None, path="patched.py"):
    """创建一个需要修复文件存在的目标并生成真实失败验收。"""
    run = app.start(GoalSpec("login", "修复登录", "错误密码必须返回 401", (
        AcceptanceCriterion("file", "指定文件必须存在", "file_exists", {"path": path}),
    )), policy or GoalBudget())
    app.begin_execution(run.id)
    app.begin_verification(run.id)
    assert app.collect_and_verify(run.id).status == "verifying"
    return run.id


def test_budget_defaults_and_validation():
    """轮次和用量限制拒绝负值、布尔值与非有限数字。"""
    assert GoalBudget().max_rounds == 3
    for params in ({"max_rounds": 0}, {"max_rounds": True}, {"max_tokens": -1},
                   {"max_tokens": True}, {"max_elapsed_seconds": 0},
                   {"max_elapsed_seconds": float("nan")},
                   {"max_cost_usd": float("inf")}, {"max_cost_usd": -1}):
        with pytest.raises(ValueError):
            GoalBudget(**params)


def test_account_persists_and_restarts(context):
    """预算配置和消耗记录在重新创建存储器后仍然完整。"""
    app, _, _, clock, root = context
    gid = goal(app, policy=GoalBudget(max_rounds=2, max_tokens=100, max_cost_usd=1.0, max_elapsed_seconds=60))
    app.record_usage(gid, "usage:one", tokens=40, cost_usd=0.25, source="llm")
    reopened = GoalBudgetManager(BudgetStore(root / "budgets"), clock=clock)
    account = reopened.store.get(gid)
    assert account.policy.max_rounds == 2
    assert len(account.events) == 1
    decision = reopened.check(gid, used_rounds=1)
    assert decision.allowed
    assert decision.remaining_rounds == 1
    assert decision.remaining_tokens == 60
    assert decision.remaining_cost_usd == 0.75
    assert decision.remaining_seconds == 60.0


def test_duplicate_usage_event_idempotent(context):
    """重启前后重复上报同一计费事件不会累计两次。"""
    app, _, _, clock, root = context
    gid = goal(app, policy=GoalBudget(max_tokens=100))
    app.record_usage(gid, "llm:001", tokens=20, cost_usd=0.2, source="provider")
    app.record_usage(gid, "llm:001", tokens=20, cost_usd=0.2, source="provider")
    reopened = GoalBudgetManager(BudgetStore(root / "budgets"), clock=clock)
    reopened.record_usage(gid, "llm:001", tokens=20, cost_usd=0.2, source="provider")
    assert reopened.check(gid, used_rounds=0).used_tokens == 20
    assert len(reopened.store.get(gid).events) == 1


def test_conflicting_usage_id_is_rejected(context):
    """同一个事件 ID 如果改了成本、Token 或来源必须拒绝上报。"""
    app, _, _, _, _ = context
    gid = goal(app)
    app.record_usage(gid, "same", tokens=2, source="a")
    with pytest.raises(ValueError):
        app.record_usage(gid, "same", tokens=3, source="a")
    with pytest.raises(ValueError):
        app.record_usage(gid, "same", tokens=2, source="b")


def test_bad_usage_values_are_rejected(context):
    """拒绝错误事件编号和不可信的计费数字。"""
    app, _, _, _, _ = context
    gid = goal(app)
    for options in ({"tokens": -1}, {"tokens": True}, {"cost_usd": float("nan")},
                    {"cost_usd": float("inf")}, {"cost_usd": -0.1}):
        with pytest.raises(ValueError):
            app.record_usage(gid, "test", **options)
    with pytest.raises(ValueError):
        app.record_usage(gid, "../../bad")
    with pytest.raises(ValueError):
        app.record_usage(gid, "good", source=" ")


def test_token_cap_prevents_repair_and_marks_exhausted(context):
    """实际已用 Token 达到上限后不能创建修复单，并将目标状态保存为 exhausted。"""
    app, _, workflow, _, _ = context
    gid = goal(app, policy=GoalBudget(max_tokens=20))
    app.record_usage(gid, "llm:1", tokens=20)
    with pytest.raises(BudgetExceededError, match="token_limit"):
        app.prepare_repair(gid)
    assert app.get(gid).status == "exhausted"
    assert app.repair_store.list_for_goal(gid) == []
    assert workflow.store.list_runs() == []


def test_cost_cap_uses_decimal_sum(context):
    """将两次 0.1 和 0.2 的费用精确判定为达到 0.3 上限。"""
    app, _, _, _, _ = context
    gid = goal(app, policy=GoalBudget(max_cost_usd=0.3))
    app.record_usage(gid, "call-1", cost_usd=0.1)
    app.record_usage(gid, "call-2", cost_usd=0.2)
    with pytest.raises(BudgetExceededError, match="cost_limit"):
        app.prepare_repair(gid)


def test_elapsed_time_cap_with_fake_clock(context):
    """即使没有真实等待，时间超过上限也会停止下一轮修复。"""
    app, _, _, clock, _ = context
    gid = goal(app, policy=GoalBudget(max_elapsed_seconds=10))
    clock.advance(11)
    with pytest.raises(BudgetExceededError, match="time_limit"):
        app.prepare_repair(gid)
    assert app.get(gid).status == "exhausted"


def test_clock_rollback_refuses_new_repair(context):
    """系统时钟明显回拨时拒绝以虚假的剩余时间继续执行。"""
    app, _, _, clock, _ = context
    gid = goal(app, policy=GoalBudget(max_elapsed_seconds=10))
    clock.advance(-10)
    with pytest.raises(BudgetExceededError, match="clock_uncertain"):
        app.prepare_repair(gid)


def test_budget_checked_again_on_dispatch(context):
    """修复单准备之后产生新的费用时，在真正调用 V7 前再次拦截。"""
    app, _, workflow, _, _ = context
    gid = goal(app, policy=GoalBudget(max_tokens=5))
    ticket = app.prepare_repair(gid)
    app.record_usage(gid, "llm-call", tokens=5)
    with pytest.raises(BudgetExceededError, match="token_limit"):
        app.dispatch_repair(gid, ticket.id)
    assert app.get(gid).status == "exhausted"
    assert app.repair_store.get(gid, ticket.id).status == "failed"
    assert workflow.store.list_runs() == []


def test_timeout_checked_again_on_dispatch(context):
    """预算在排队期间过期时不能启动原先准备好的修复。"""
    app, _, workflow, clock, _ = context
    gid = goal(app, policy=GoalBudget(max_elapsed_seconds=5))
    ticket = app.prepare_repair(gid)
    clock.advance(5)
    with pytest.raises(BudgetExceededError, match="time_limit"):
        app.dispatch_repair(gid, ticket.id)
    assert not workflow.store.list_runs()


def test_max_rounds_one_stops_second_attempt(context):
    """即使第一轮 Workflow 正常结束，验收仍失败也不能超出修复轮次。"""
    app, repo, workflow, _, _ = context
    gid = goal(app, policy=GoalBudget(max_rounds=1, max_tokens=100))
    workflow.actions.register("test.other", lambda ctx: {"result": "unchanged"})
    app.repairs.plan = lambda feedback: WorkflowDefinition("noop", "无修改", (StepDefinition("x", action="test.noop"),))
    ticket = app.dispatch_repair(gid, app.prepare_repair(gid).id)
    assert app.advance_repair(gid, ticket.id).status == "completed"
    assert app.collect_and_verify(gid).status == "verifying"
    with pytest.raises(BudgetExceededError, match="round_limit"):
        app.prepare_repair(gid)
    assert app.get(gid).status == "exhausted"


def test_real_repair_and_verified_goal(context):
    """真实 Git 修复通过 V7 工作流完成后用新证据验证 Goal。"""
    app, repo, workflow, clock, _ = context
    gid = goal(app, policy=GoalBudget(max_rounds=2, max_tokens=1000, max_cost_usd=1.0))
    ticket = app.prepare_repair(gid)
    assert ticket.status == "prepared"
    ticket = app.dispatch_repair(gid, ticket.id)
    assert ticket.status == "running"
    assert app.advance_repair(gid, ticket.id).status == "completed"
    assert app.get(gid).status == "verifying"
    clock.advance(1)
    assert app.collect_and_verify(gid).status == "verified"
    assert (repo / "patched.py").exists()
    assert git(repo, "status", "--porcelain") == ""
    with pytest.raises(ValueError):
        app.prepare_repair(gid)


def test_running_workflow_not_aborted_by_usage_change(context):
    """预算已耗尽也不重放或中止正在执行的外部副作用。"""
    app, _, _, clock, _ = context
    gid = goal(app, policy=GoalBudget(max_tokens=1))
    ticket = app.dispatch_repair(gid, app.prepare_repair(gid).id)
    app.record_usage(gid, "expensive", tokens=3)
    assert app.budget_status(gid).allowed is False
    assert app.advance_repair(gid, ticket.id).status == "completed"
    clock.advance(1)
    assert app.collect_and_verify(gid).status == "verified"


def test_duplicate_dispatch_not_replayed(context):
    """已有 V7 工作流的修复单不能再次启动重复任务。"""
    app, _, workflow, _, _ = context
    gid = goal(app)
    ticket = app.dispatch_repair(gid, app.prepare_repair(gid).id)
    with pytest.raises(ValueError):
        app.dispatch_repair(gid, ticket.id)
    assert len(workflow.store.list_runs()) == 1


def test_restart_application_keeps_policy(context):
    """重新创建 GoalApplication 后仍遵守此前持久化的预算。"""
    app, repo, workflow, clock, root = context
    gid = goal(app, policy=GoalBudget(max_tokens=5))
    app.record_usage(gid, "token-event", tokens=5)
    reopened = GoalApplication(root, repo, workflow, app.repairs.plan, clock=clock)
    assert reopened.budget_status(gid).remaining_tokens == 0
    with pytest.raises(BudgetExceededError):
        reopened.prepare_repair(gid)


def test_verifier_unknown_does_not_launch_repair(context):
    """只缺少证据而没有明确失败时禁止消耗预算启动代码修复。"""
    app, _, workflow, _, _ = context
    run = app.start(GoalSpec("manual", "人工", "需要人工确认", (
        AcceptanceCriterion("review", "必须人工审批", "manual", {"instructions": "请检查"}),
    )), GoalBudget())
    app.begin_execution(run.id)
    app.begin_verification(run.id)
    assert app.collect_and_verify(run.id).status == "unknown"
    with pytest.raises(ValueError):
        app.prepare_repair(run.id)
    assert not workflow.store.list_runs()


def test_budget_account_conflict(context):
    """过期账本不能覆盖其他操作写入的最新版本。"""
    app, _, _, _, _ = context
    gid = goal(app)
    old = app.budget_store.get(gid)
    app.record_usage(gid, "new", tokens=1)
    with pytest.raises(BudgetConflictError):
        app.budget_store.save(old)


def test_budget_store_rejects_path_escape(context):
    """预算存储不允许通过外部路径访问非 Goal 数据。"""
    app, _, _, _, _ = context
    with pytest.raises(ValueError):
        app.budget_store.get("../../escaped")


def test_budget_cannot_be_replaced(context):
    """已有 Goal 的预算不能在执行中被新的策略覆盖。"""
    app, _, _, _, _ = context
    gid = goal(app)
    with pytest.raises(FileExistsError):
        app.budgets.open(gid, GoalBudget(max_rounds=999))


def test_forged_duplicate_event_detected(context):
    """磁盘数据如果出现重复账单 ID 应当拒绝读取而非悄悄重复扣费。"""
    app, _, _, _, _ = context
    gid = goal(app)
    app.record_usage(gid, "first", tokens=3)
    path = app.budget_store.root / f"{gid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["events"].append(data["events"][0])
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        app.budget_store.get(gid)


def test_store_requires_non_naive_clock(context, tmp_path):
    """预算管理必须使用带时区时钟，避免不同地区误判耗时。"""
    app, _, _, _, _ = context
    gid = goal(app)
    manager = GoalBudgetManager(app.budget_store, clock=lambda: datetime(2026, 10, 29))
    with pytest.raises(ValueError):
        manager.check(gid, used_rounds=0)


def test_multiple_events_roundtrip(context):
    """预算账本能够累计多个独立来源的 Token 和费用并准确恢复。"""
    app, _, _, _, _ = context
    gid = goal(app, policy=GoalBudget(max_tokens=12, max_cost_usd=0.7))
    app.record_usage(gid, "lead:1", tokens=2, cost_usd=0.1, source="lead")
    app.record_usage(gid, "worker:1", tokens=4, cost_usd=0.2, source="worker")
    d = app.budget_status(gid)
    assert d.used_tokens == 6 and d.remaining_tokens == 6
    assert d.used_cost_usd == pytest.approx(0.3)
    assert d.remaining_cost_usd == pytest.approx(0.4)


def test_goal_without_budget_fails_closed(context):
    """直接创建而没有预算账本的 Goal 不能通过统一入口执行。"""
    app, _, _, _, _ = context
    run = app.goals.start(GoalSpec("bypass", "缺失预算", "没有预算应拒绝", (
        AcceptanceCriterion("f", "文件", "file_exists", {"path": "a"}),
    )))
    with pytest.raises(FileNotFoundError):
        app.begin_execution(run.id)


def test_v7_6_workflow_application_real_integration(context):
    """使用 V7.6 正式 WorkflowApplication 入口执行真实 Git 修复并完成 Goal 验收。"""
    from devpilot.workflow import WorkflowApplication, WorkflowBindings
    from devpilot.workflow.external import ExternalOutcome

    _, repo, _, clock, root = context
    bindings = WorkflowBindings(
        dispatch_tool=lambda name, args: {"tool": name},
        create_task=lambda data: "stub-task",
        run_assignment=lambda worker, task: {"worker": worker},
        submit_job=lambda name, args: "stub-job",
        query_job=lambda job_id: ExternalOutcome("succeeded", output={"id": job_id}),
        merge_worktrees=lambda ctx: {"merge": True},
        allowed_tools=frozenset({"file.read"}),
        allowed_mcp_tools=frozenset({"mcp__stub__read"}),
    )
    with WorkflowApplication(root / "official-v7-workflows", bindings) as official:
        def fix(ctx):
            """在真实 Git 工作区修改代码并提交，模拟完成后的可验收交付。"""
            (repo / "patched.py").write_text("def result():\n    return 401\n", encoding="utf-8")
            git(repo, "add", "patched.py")
            git(repo, "-c", "user.name=T", "-c", "user.email=test@example.invalid", "commit", "-qm", "fix")
            return {"ok": True}

        official.actions.register("fix.git", fix)

        def plan(feedback):
            """仅选择已注册的安全测试动作作为修复步骤。"""
            return WorkflowDefinition("official", "官方 V7 入口", (StepDefinition("fix", action="fix.git"),))

        app = GoalApplication(root / "official-goals", repo, official, plan, clock=clock)
        gid = goal(app, policy=GoalBudget(max_rounds=1))
        ticket = app.dispatch_repair(gid, app.prepare_repair(gid).id)
        assert app.advance_repair(gid, ticket.id).status == "completed"
        clock.advance(1)
        assert app.collect_and_verify(gid).status == "verified"
        assert app.budget_status(gid).remaining_rounds == 0


def test_goal_resume_after_dispatch_unknown_is_not_automatic(context):
    """启动结果未知时预算系统不会猜测是否成功，也不会发起第二条工作流。"""
    app, _, workflow, _, root = context
    gid = goal(app)

    class LostReturn:
        """模拟 V7 成功创建 Workflow 但返回响应丢失。"""

        store = workflow.store

        def start(self, definition, inputs):
            """先提交成功再模拟网络断开，要求从 V7 Store 对账。"""
            workflow.start(definition, inputs)
            raise ConnectionError("lost return")

    app.repairs.workflow = LostReturn()
    ticket = app.prepare_repair(gid)
    with pytest.raises(ConnectionError):
        app.dispatch_repair(gid, ticket.id)
    assert app.repair_store.get(gid, ticket.id).status == "dispatching"
    assert len(workflow.store.list_runs()) == 1
    reopened = GoalApplication(root, app.collector.workspace, workflow, app.repairs.plan)
    with pytest.raises(ValueError):
        reopened.prepare_repair(gid)
    with pytest.raises(ValueError):
        reopened.dispatch_repair(gid, ticket.id)
    assert len(workflow.store.list_runs()) == 1


def test_budget_store_rejects_policy_changes(context):
    """已经创建的 Goal 不允许通过保存旧账本偷偷提高预算上限。"""
    app, _, _, _, _ = context
    gid = goal(app, policy=GoalBudget(max_rounds=1))
    old = app.budget_store.get(gid)
    with pytest.raises(ValueError, match="cannot be changed"):
        app.budget_store.save(replace(old, policy=GoalBudget(max_rounds=999)))


def test_budget_store_keeps_append_only_usage(context):
    """已经记录的资源消耗不能通过替换账本删除或修改。"""
    app, _, _, _, _ = context
    gid = goal(app)
    app.record_usage(gid, "real-usage", tokens=42)
    account = app.budget_store.get(gid)
    with pytest.raises(ValueError, match="append-only"):
        app.budget_store.save(replace(account, events=()))
    assert app.budget_status(gid).used_tokens == 42
