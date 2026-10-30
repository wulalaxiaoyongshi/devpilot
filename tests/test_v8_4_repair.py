"""V8.4：使用真实 V7 Workflow、Git 证据和 GoalStore 验证反馈修复闭环。"""

import subprocess
from pathlib import Path

import pytest

from devpilot.goal import (
    AcceptanceCriterion, EvidenceCollector, EvidenceStore, FeedbackGenerator,
    GoalRuntime, GoalSpec, GoalStore, GoalVerifier, RepairController, RepairStore,
)
from devpilot.goal.repair_store import RepairConflictError
from devpilot.workflow import ActionRegistry, StepDefinition, WorkflowDefinition, WorkflowEngine, WorkflowStore


def git(repo: Path, *args: str) -> str:
    """运行只针对隔离测试仓库的 Git 命令并返回结果。"""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    """准备真实 Git 仓库、V8.1～V8.3 数据存储以及真正的 V7 WorkflowEngine。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "readme.txt").write_text("hello\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "readme.txt")
    git(repo, "-c", "user.name=Tester", "-c", "user.email=test@example.invalid", "commit", "-qm", "init")
    store = GoalStore(tmp_path / "state" / "goals")
    evidence = EvidenceStore(tmp_path / "state" / "evidence")
    goals = GoalRuntime(store)
    collector = EvidenceCollector(store, evidence, repo)
    verifier = GoalVerifier(store, evidence, repo)
    actions = ActionRegistry()
    workflow = WorkflowEngine(WorkflowStore(tmp_path / "state" / "workflows"), actions)
    repairs = RepairStore(tmp_path / "state" / "repairs")
    return repo, goals, collector, verifier, actions, workflow, repairs


def failing_goal(env, *, manual=False):
    """通过真实证据采集和 V8.3 验收生成一条明确失败的 Goal。"""
    repo, goals, collector, verifier, *_ = env
    checks = [AcceptanceCriterion("target", "必须存在 patched.py", "file_exists", {"path": "patched.py"})]
    if manual:
        checks.append(AcceptanceCriterion("human", "人工复核", "manual", {"instructions": "请审核"}))
    spec = GoalSpec("repair_bug", "修复登录缺陷", "修复后必须存在 patched.py", tuple(checks))
    run = goals.start(spec)
    goals.begin_execution(run.id)
    goals.begin_verification(run.id)
    collector.collect_all(run.id)
    report = verifier.verify(run.id)
    assert report.criteria[0].status == "fail"
    return run.id


def controller(env, *, max_rounds=3, action="repair", workflow=None):
    """用可注入的确定性 V7 动作创建修复控制器。"""
    repo, goals, collector, verifier, actions, normal, repairs = env
    if action == "repair":
        def handler(ctx):
            """生成真正的修复文件并提交 Git，模拟 Worker 的可验证代码修改。"""
            (repo / "patched.py").write_text("def patched():\n    return True\n", encoding="utf-8")
            git(repo, "add", "patched.py")
            git(repo, "-c", "user.name=Tester", "-c", "user.email=test@example.invalid", "commit", "-qm", "repair")
            return {"commit": git(repo, "rev-parse", "HEAD")}
    elif action == "failure":
        def handler(ctx):
            """模拟修复动作发生明确故障。"""
            raise RuntimeError("repair failed")
    else:
        def handler(ctx):
            """模拟修复步骤不改变代码但成功执行。"""
            return {"ok": True}
    actions.register("goal.repair", handler)

    def make_plan(feedback):
        """根据宿主信任的动作模板生成 V7 Workflow，而不是执行证据中的文字。"""
        return WorkflowDefinition("repair-workflow", "修复任务", (StepDefinition("fix", action="goal.repair"),))

    return RepairController(goals, repairs, workflow or normal, make_plan, max_rounds=max_rounds)


def test_full_real_goal_repair_and_fresh_verification(env):
    """失败证据会启动真正 V7 修复，提交代码后必须用新证据才能 verified。"""
    run_id = failing_goal(env, manual=True)
    ctl = controller(env)
    ticket = ctl.prepare(run_id)
    assert ticket.status == "prepared"
    assert ticket.feedback.failures[0].criterion_id == "target"
    assert ticket.feedback.unknowns[0].criterion_id == "human"
    ticket = ctl.dispatch(run_id, ticket.id)
    assert ticket.status == "running"
    assert env[1].get(run_id).status == "repairing"
    assert ctl.poll(run_id, ticket.id).status == "running"
    workflow_run = env[5].run_until_blocked(ticket.workflow_run_id)
    assert workflow_run.status == "completed"
    assert ctl.poll(run_id, ticket.id).status == "completed"
    assert env[1].get(run_id).status == "verifying"
    assert env[1].get(run_id).criteria["target"].status == "unverified"
    assert env[3].verify(run_id).status == "unknown"  # 旧证据不允许验收
    env[1].resume_verification(run_id)
    env[2].collect_all(run_id)
    report = env[3].verify(run_id)
    assert report.status == "unknown"  # 手动验收尚未完成，不能宣称全通过
    assert next(c.status for c in report.criteria if c.criterion_id == "target") == "pass"
    assert env[6].get(run_id, ticket.id).status == "completed"


def test_all_automatic_conditions_can_verify_after_repair(env):
    """没有人工验收项时，修复后重新采集真实 Git 证据即可认证 Goal。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = ctl.dispatch(run_id, ctl.prepare(run_id).id)
    env[5].run_until_blocked(ticket.workflow_run_id)
    ctl.poll(run_id, ticket.id)
    env[2].collect_all(run_id)
    assert env[3].verify(run_id).status == "verified"
    with pytest.raises(ValueError):
        ctl.prepare(run_id)


def test_feedback_preserves_exact_reason_and_evidence_id(env):
    """反馈应引用 V8.3 存储的失败原因与真实证据编号。"""
    run_id = failing_goal(env)
    run = env[1].get(run_id)
    feedback = FeedbackGenerator(env[1].store).build(run_id)
    assert feedback.failures[0].reason == run.criteria["target"].reason
    assert feedback.failures[0].evidence_id == run.criteria["target"].evidence_ids[0]
    assert "failed_criteria" in feedback.as_context()


def test_unknown_without_fail_cannot_trigger_repair(env):
    """没有证据只得到 UNKNOWN 时只能补充检查而不能发起自动修复。"""
    goals = env[1]
    spec = GoalSpec("missing_evidence", "缺乏证据", "请先采集证据", (AcceptanceCriterion("file", "文件", "file_exists", {"path": "readme.txt"}),))
    run = goals.start(spec)
    goals.begin_execution(run.id)
    goals.begin_verification(run.id)
    assert env[3].verify(run.id).status == "unknown"
    with pytest.raises(ValueError):
        controller(env).prepare(run.id)


def test_prepared_ticket_survives_restart(env):
    """重启后仍能读取修复单，并且不能因为重建 Controller 就重复创建。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    prepared = ctl.prepare(run_id)
    reopened = RepairStore(env[6].root)
    assert reopened.get(run_id, prepared.id).status == "prepared"
    with pytest.raises(ValueError, match="active"):
        RepairController(env[1], reopened, env[5], ctl.plan).prepare(run_id)


def test_dispatch_is_single_shot(env):
    """同一个修复单提交一次后，不允许再次自动启动第二条 Workflow。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = ctl.prepare(run_id)
    ctl.dispatch(run_id, ticket.id)
    with pytest.raises(ValueError):
        ctl.dispatch(run_id, ticket.id)
    assert len(env[5].store.list_runs()) == 1


def test_dispatch_result_unknown_can_reconcile(env):
    """V7 已创建工作流但响应丢失时，只能查找并绑定原 Workflow，而非重新提交。"""
    run_id = failing_goal(env)
    class LostResponse:
        """模拟 V7 实际创建成功但连接随后断开的入口。"""
        store = env[5].store

        def start(self, definition, inputs):
            """先创建真实 Workflow，再抛出连接异常模拟丢失返回值。"""
            env[5].start(definition, inputs)
            raise ConnectionError("lost response")
    ctl = controller(env, workflow=LostResponse())
    ticket = ctl.prepare(run_id)
    with pytest.raises(ConnectionError):
        ctl.dispatch(run_id, ticket.id)
    assert env[6].get(run_id, ticket.id).status == "dispatching"
    assert len(env[5].store.list_runs()) == 1
    with pytest.raises(RuntimeError):
        ctl.poll(run_id, ticket.id)
    original_run = env[5].store.list_runs()[0]
    assert ctl.link_dispatched(run_id, ticket.id, original_run.id).status == "running"
    assert env[1].get(run_id).status == "repairing"


def test_mismatched_workflow_ticket_rejected(env):
    """宿主不能把其他任务的 Workflow ID 关联到本修复单。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = env[6].update(ctl.prepare(run_id), status="dispatching")
    other_run = env[5].start(WorkflowDefinition("unrelated", "不相关任务", (StepDefinition("step", action="goal.repair"),)))
    with pytest.raises(ValueError):
        ctl.link_dispatched(run_id, ticket.id, other_run.id)


def test_stale_goal_version_blocks_dispatch(env):
    """修复单创建后 Goal 被修改，旧修复单不能再启动。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = ctl.prepare(run_id)
    env[1].add_note(run_id, "需求或审批状态变化")
    with pytest.raises(ValueError):
        ctl.dispatch(run_id, ticket.id)
    assert env[6].get(run_id, ticket.id).status == "prepared"


def test_failed_workflow_blocks_goal(env):
    """修复工作流明确失败时，Goal 应变成 blocked 而不是伪造完成。"""
    run_id = failing_goal(env)
    ctl = controller(env, action="failure")
    ticket = ctl.dispatch(run_id, ctl.prepare(run_id).id)
    assert env[5].run_until_blocked(ticket.workflow_run_id).status == "failed"
    assert ctl.poll(run_id, ticket.id).status == "failed"
    assert env[1].get(run_id).status == "blocked"


def test_round_cap_requires_new_policy(env):
    """修复轮次达到上限后不能无限发起相同修复。"""
    run_id = failing_goal(env)
    ctl = controller(env, max_rounds=1, action="noop")
    ticket = ctl.dispatch(run_id, ctl.prepare(run_id).id)
    env[5].run_until_blocked(ticket.workflow_run_id)
    ctl.poll(run_id, ticket.id)
    env[2].collect_all(run_id)
    env[3].verify(run_id)
    with pytest.raises(ValueError, match="limit"):
        ctl.prepare(run_id)


def test_abandon_unknown_dispatch_requires_host_reason(env):
    """结果未知的提交必须明确由宿主核实后才能放弃。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = env[6].update(ctl.prepare(run_id), status="dispatching")
    with pytest.raises(ValueError):
        ctl.abandon_dispatch(run_id, ticket.id, "")
    assert ctl.abandon_dispatch(run_id, ticket.id, "已检查 V7 Store 未创建对应运行").status == "failed"
    assert env[1].get(run_id).status == "verifying"


def test_repair_store_optimistic_conflict(env):
    """两份不同版本的同一修复单不能相互覆盖。"""
    run_id = failing_goal(env)
    ticket = controller(env).prepare(run_id)
    env[6].update(ticket, status="dispatching")
    with pytest.raises(RepairConflictError):
        env[6].update(ticket, status="failed")


def test_invalid_ids_rejected_by_repair_store(env):
    """修复单文件名只能使用受校验的 UUID，不允许路径穿越。"""
    with pytest.raises(ValueError):
        env[6].get("../../escape", "0" * 32)
    with pytest.raises(ValueError):
        env[6].get("0" * 32, "../../escape")


def test_no_second_prepare_during_running(env):
    """修复任务仍运行时不允许提交同一个 Goal 的第二轮修复。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = ctl.dispatch(run_id, ctl.prepare(run_id).id)
    with pytest.raises(ValueError):
        ctl.prepare(run_id)
    assert env[6].get(run_id, ticket.id).status == "running"


def test_poll_after_completion_is_idempotent(env):
    """修复完成后重复轮询不会反复清空证据或再次开始验收轮次。"""
    run_id = failing_goal(env)
    ctl = controller(env)
    ticket = ctl.dispatch(run_id, ctl.prepare(run_id).id)
    env[5].run_until_blocked(ticket.workflow_run_id)
    ctl.poll(run_id, ticket.id)
    events = len(env[1].get(run_id).events)
    ctl.poll(run_id, ticket.id)
    assert len(env[1].get(run_id).events) == events
