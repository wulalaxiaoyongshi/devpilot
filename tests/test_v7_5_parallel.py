"""V7.5 真实线程测试：验证并行、依赖汇合、资源限制和 V7.4 状态兼容。"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from devpilot.workflow import (
    ActionRegistry, ApprovalPolicy, ExternalOutcome, ExternalResolverRegistry,
    ExternalSubmission, ParallelWorkflowRunner, RetryPolicy, RetryableActionError,
    StepDefinition, UncertainActionError, WorkflowDefinition, WorkflowEngine,
    WorkflowStore, validate_definition,
)


def make_engine(tmp_path, handlers, resolver=None, clock=None):
    """创建带真实磁盘 WorkflowStore 的 Engine，并注册给定的测试动作。"""
    actions = ActionRegistry()
    for name, handler in handlers.items():
        actions.register(name, handler)
    external = ExternalResolverRegistry()
    if resolver is not None:
        external.register("test", resolver)
    return WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external, clock=clock)


def flow(*steps):
    """把测试步骤组合为一个可持久化的 Workflow 定义。"""
    return WorkflowDefinition("parallel", "并行测试", steps)


def test_two_workers_really_overlap_and_join_receives_both_results(tmp_path):
    """两个 Worker 必须同时运行，汇合节点只有在两个分支成功后才能读取它们的输出。"""
    barrier = threading.Barrier(2)
    started = set()
    lock = threading.Lock()

    def branch(ctx):
        """记录线程并等待另一个分支到达同一位置，以证明真正并发执行。"""
        with lock:
            started.add(ctx.step_id)
        barrier.wait(timeout=3)
        return {"value": ctx.step_id}

    def join(ctx):
        """收到两个上游完成结果后构造最终汇总。"""
        assert ctx.results["left"]["value"] == "left"
        assert ctx.results["right"]["value"] == "right"
        return {"both": True}

    engine = make_engine(tmp_path, {"branch": branch, "join": join})
    run = engine.start(flow(StepDefinition("left", action="branch"), StepDefinition("right", action="branch"), StepDefinition("merge", action="join", depends_on=("left", "right"))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        result = runner.run_until_blocked(run.id, timeout_seconds=5)
        assert result.status == "completed"
        assert result.steps["merge"].output == {"both": True}
        assert started == {"left", "right"}


def test_max_workers_limits_global_concurrency(tmp_path):
    """无论有多少可运行步骤，线程同时执行数都不能超过 max_workers。"""
    lock = threading.Lock()
    current = 0
    peak = 0

    def work(ctx):
        """用共享计数器记录实际进入执行函数的线程峰值。"""
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.025)
        with lock:
            current -= 1
        return {"ok": True}

    engine = make_engine(tmp_path, {"work": work})
    run = engine.start(flow(*(StepDefinition(f"step{i}", action="work") for i in range(8))))
    with ParallelWorkflowRunner(engine, max_workers=3) as runner:
        assert runner.run_until_blocked(run.id, timeout_seconds=5).status == "completed"
    assert 1 < peak <= 3


def test_exclusive_resource_prevents_concurrent_writers_without_blocking_other_branch(tmp_path):
    """同一独占资源只能有一个运行者，排在后面的独立步骤仍能及时启动。"""
    gate = threading.Event()
    entered = threading.Event()
    other_entered = threading.Event()
    counter = {"same": 0}

    def first(ctx):
        """占用 repo:main 资源直到测试释放同步事件。"""
        entered.set()
        assert gate.wait(timeout=3)
        return {"ok": True}

    def second(ctx):
        """检查第二个写入者只能在第一个完成之后启动。"""
        counter["same"] += 1
        return {"ok": True}

    def other(ctx):
        """记录没有资源冲突的步骤已经真正启动。"""
        other_entered.set()
        return {"ok": True}

    engine = make_engine(tmp_path, {"first": first, "second": second, "other": other})
    run = engine.start(flow(
        StepDefinition("first", action="first", exclusive_resources=("repo:main",)),
        StepDefinition("second", action="second", exclusive_resources=("repo:main",)),
        StepDefinition("other", action="other", exclusive_resources=("repo:tests",)),
    ))
    with ParallelWorkflowRunner(engine, max_workers=3) as runner:
        try:
            runner.tick(run.id)
            assert entered.wait(timeout=2)
            assert other_entered.wait(timeout=2)
            assert counter["same"] == 0
            assert run.id and (run.id, "second") not in runner.active_steps()
        finally:
            gate.set()
        assert runner.run_until_blocked(run.id, timeout_seconds=5).status == "completed"
        assert counter["same"] == 1


def test_resource_lock_is_global_across_workflow_runs(tmp_path):
    """同一个 Runner 上不同 Workflow 也必须竞争相同的资源名。"""
    entered = threading.Event()
    gate = threading.Event()
    second_started = threading.Event()

    def first(ctx):
        """占用共享数据库资源直到测试允许完成。"""
        entered.set()
        assert gate.wait(timeout=3)
        return {"ok": True}

    def second(ctx):
        """记录第二条 Workflow 获得共享资源的时刻。"""
        second_started.set()
        return {"ok": True}

    engine = make_engine(tmp_path, {"a": first, "b": second})
    a = engine.start(flow(StepDefinition("a", action="a", exclusive_resources=("db",))))
    b = engine.start(flow(StepDefinition("b", action="b", exclusive_resources=("db",))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        try:
            runner.tick(a.id)
            assert entered.wait(timeout=2)
            runner.tick(b.id)
            assert not second_started.is_set()
        finally:
            gate.set()
        assert runner.run_until_blocked(a.id, timeout_seconds=5).status == "completed"
        assert runner.run_until_blocked(b.id, timeout_seconds=5).status == "completed"
        assert second_started.is_set()


def test_approval_blocks_join_but_does_not_stop_independent_action(tmp_path):
    """审批关卡暂停受依赖的汇合步骤，但不影响另一条独立分支完成。"""
    acts = []

    def work(ctx):
        """记录实际执行的分支并返回成功。"""
        acts.append(ctx.step_id)
        return {"ok": True}

    engine = make_engine(tmp_path, {"work": work})
    run = engine.start(flow(
        StepDefinition("approval", kind="approval", approval=ApprovalPolicy(approvers=("reviewer",))),
        StepDefinition("independent", action="work"),
        StepDefinition("finish", action="work", depends_on=("approval", "independent")),
    ))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        first = runner.run_until_blocked(run.id)
        assert first.status == "waiting_approval"
        assert first.steps["independent"].status == "succeeded"
        assert first.steps["finish"].status == "pending"
        request = engine.get_approval_request(run.id, "approval")
        engine.approve_step(run.id, "approval", True, actor="reviewer", request_id=request.request_id)
        assert runner.run_until_blocked(run.id).status == "completed"
        assert acts == ["independent", "finish"]


def test_external_job_waits_for_explicit_success_before_join(tmp_path):
    """异步提交只会进入 waiting_external，必须轮询得到成功才可以汇合。"""
    statuses = ["running", "succeeded"]

    def submit(ctx):
        """返回 V7.2 已定义的外部 Job 凭据。"""
        return ExternalSubmission("test", "job-7")

    def done(ctx):
        """记录最终汇合步骤真的看见了外部 Job 的结果。"""
        assert ctx.results["submit"]["result"] == {"test": "ok"}
        return {"done": True}

    def resolve(ref, meta):
        """按测试指定的顺序返回 Job 运行中及成功状态。"""
        status = statuses.pop(0)
        return ExternalOutcome(status, output={"test": "ok"} if status == "succeeded" else {})

    engine = make_engine(tmp_path, {"submit": submit, "done": done}, resolver=resolve)
    run = engine.start(flow(StepDefinition("submit", action="submit"), StepDefinition("end", action="done", depends_on=("submit",))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        first = runner.run_until_blocked(run.id)
        assert first.status == "waiting_external"
        assert first.steps["end"].status == "pending"
        engine.poll_external(run.id, "submit")
        assert runner.run_until_blocked(run.id).steps["end"].status == "pending"
        engine.poll_external(run.id, "submit")
        assert runner.run_until_blocked(run.id).status == "completed"


def test_failed_step_prevents_new_steps_but_collects_already_running(tmp_path):
    """一条分支失败后停止提交新的步骤，但仍安全保存已开始分支的完成结果。"""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fail(ctx):
        """立即报告本分支的明确失败。"""
        return {"invalid": object()}

    def blocked(ctx):
        """运行中的步骤等待测试释放后才结束。"""
        started.set()
        assert release.wait(timeout=3)
        return {"ok": True}

    def third(ctx):
        """如果被意外执行则留下明显的调用记录。"""
        calls.append("third")
        return {"ok": True}

    engine = make_engine(tmp_path, {"fail": fail, "blocked": blocked, "third": third})
    run = engine.start(flow(StepDefinition("bad", action="fail"), StepDefinition("runningstep", action="blocked"), StepDefinition("third", action="third")))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        try:
            runner.tick(run.id)
            assert started.wait(timeout=2)
            time.sleep(0.02)
            assert runner.tick(run.id).status == "failed"
            assert calls == []
        finally:
            release.set()
        assert runner.run_until_blocked(run.id, timeout_seconds=5).status == "failed"
        assert engine.store.get(run.id).steps["runningstep"].status == "succeeded"
        assert engine.store.get(run.id).steps["third"].status == "pending"


def test_retryable_error_reuses_v7_4_retry_policy(tmp_path):
    """线程中遇到明确可重试错误时仍使用 V7.4 的安全重试状态和稳定幂等键。"""
    calls = []

    def flaky(ctx):
        """首次失败、第二次成功，用于验证重试次数和幂等键。"""
        calls.append((ctx.attempt, ctx.idempotency_key))
        if len(calls) == 1:
            raise RetryableActionError("temporary")
        return {"ok": True}

    engine = make_engine(tmp_path, {"flaky": flaky})
    run = engine.start(flow(StepDefinition("work", action="flaky", max_attempts=2, retry=RetryPolicy(True, True))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        assert runner.run_until_blocked(run.id, timeout_seconds=5).status == "completed"
        assert [n for n, _ in calls] == [1, 2]
        assert calls[0][1] == calls[1][1] == f"{run.id}:work"


def test_uncertain_action_requires_reconciliation(tmp_path):
    """执行结果未知的动作不得自动重试，也不能提前执行后续步骤。"""
    calls = []

    def unknown(ctx):
        """模拟 MCP 已发送但返回丢失的副作用操作。"""
        calls.append(ctx.step_id)
        raise UncertainActionError("response lost")

    engine = make_engine(tmp_path, {"unknown": unknown})
    run = engine.start(flow(StepDefinition("issue", action="unknown", retry=RetryPolicy(idempotent=False)), StepDefinition("after", action="unknown", depends_on=("issue",))))
    with ParallelWorkflowRunner(engine) as runner:
        assert runner.run_until_blocked(run.id).status == "recovery_required"
        assert calls == ["issue"]
        engine.reconcile_step(run.id, "issue", "succeeded", actor="host", evidence="checked issue", output={"id": 7})
        assert runner.run_until_blocked(run.id).status == "recovery_required"
        assert calls == ["issue", "after"]


def test_step_definition_resources_roundtrip_and_old_files(tmp_path):
    """新资源字段写入 JSON 后可恢复，旧版本缺少字段时仍可正常读取。"""
    engine = make_engine(tmp_path, {"noop": lambda ctx: {"ok": True}})
    run = engine.start(flow(StepDefinition("work", action="noop", exclusive_resources=("repo:main", "db"))))
    assert engine.store.get(run.id).definition.steps[0].exclusive_resources == ("repo:main", "db")
    path = engine.store.directory / f"{run.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["definition"]["steps"][0].pop("exclusive_resources")
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert engine.store.get(run.id).definition.steps[0].exclusive_resources == ()


@pytest.mark.parametrize("resources", [("bad space",), ("db", "db"), ("",), ("$bad",)])
def test_invalid_or_duplicate_resource_names_are_rejected(resources):
    """非法资源名与重复资源名必须在 Workflow 开始之前被拒绝。"""
    definition = flow(StepDefinition("work", action="noop", exclusive_resources=resources))
    with pytest.raises(ValueError):
        validate_definition(definition)


def test_approval_cannot_claim_exclusive_resource():
    """审批节点不实际执行 Python 动作，因此不能配置独占执行资源。"""
    with pytest.raises(ValueError):
        validate_definition(flow(StepDefinition("review", kind="approval", exclusive_resources=("db",))))


def test_timeout_keeps_work_running_and_close_persists_result(tmp_path):
    """调用方的等待超时不取消真实线程，退出 Runner 时必须等待并持久化最终结果。"""
    gate = threading.Event()

    def work(ctx):
        """在等待事件期间模拟较慢的代码任务。"""
        assert gate.wait(timeout=3)
        return {"ok": True}

    engine = make_engine(tmp_path, {"work": work})
    run = engine.start(flow(StepDefinition("step", action="work")))
    with ParallelWorkflowRunner(engine) as runner:
        try:
            status = runner.run_until_blocked(run.id, timeout_seconds=0.02)
            assert status.status == "running"
            assert (run.id, "step") in runner.active_steps()
        finally:
            gate.set()
    assert engine.store.get(run.id).status == "completed"


def test_two_threads_can_tick_same_runner_without_double_submission(tmp_path):
    """同一个 Runner 被两条调度线程并发触发时，步骤最多只能被提交一次。"""
    gate = threading.Event()
    count = 0
    started = threading.Event()
    lock = threading.Lock()

    def work(ctx):
        """记录每次实际动作执行，并等待测试释放。"""
        nonlocal count
        with lock:
            count += 1
        started.set()
        assert gate.wait(timeout=3)
        return {"ok": True}

    engine = make_engine(tmp_path, {"work": work})
    run = engine.start(flow(StepDefinition("work", action="work")))
    with ParallelWorkflowRunner(engine) as runner:
        threads = [threading.Thread(target=runner.tick, args=(run.id,)) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            assert started.wait(timeout=2)
            assert count == 1
        finally:
            gate.set()
    assert engine.store.get(run.id).status == "completed"


def test_max_workers_requires_positive_int(tmp_path):
    """错误的并行上限参数不能进入线程池调度阶段。"""
    engine = make_engine(tmp_path, {"noop": lambda ctx: {"ok": True}})
    with pytest.raises(ValueError):
        ParallelWorkflowRunner(engine, max_workers=0)
    with pytest.raises(ValueError):
        ParallelWorkflowRunner(engine, max_workers=True)


def test_external_job_retains_resource_until_completion(tmp_path):
    """动作交出异步 Job ID 后仍应保留资源锁，直到外部 Job 真正结束。"""
    def submit(ctx):
        """模拟提交一个仍在后台修改同一仓库的长时间任务。"""
        return ExternalSubmission("test", "build-1")

    def modify(ctx):
        """在被允许执行时返回修改成功的结果。"""
        return {"modified": True}

    def resolve(ref, meta):
        """模拟宿主确认外部 Job 已成功结束。"""
        return ExternalOutcome("succeeded", output={"ok": True})

    engine = make_engine(tmp_path, {"submit": submit, "modify": modify}, resolver=resolve)
    first = engine.start(flow(StepDefinition("asyncstep", action="submit", exclusive_resources=("repo:main",))))
    second = engine.start(flow(StepDefinition("change", action="modify", exclusive_resources=("repo:main",))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        assert runner.run_until_blocked(first.id).status == "waiting_external"
        assert runner.run_until_blocked(second.id, timeout_seconds=0.01).steps["change"].status == "pending"
        engine.poll_external(first.id, "asyncstep")
        assert runner.run_until_blocked(second.id).status == "completed"


def test_unknown_result_retains_resource_until_reconciliation(tmp_path):
    """结果未知的步骤继续持有关键资源，避免其他步骤覆盖可能仍在执行的操作。"""
    def unknown(ctx):
        """模拟操作已经发送但连接断开的情况。"""
        raise UncertainActionError("lost connection")

    def safe(ctx):
        """返回不涉及新故障的成功结果。"""
        return {"ok": True}

    engine = make_engine(tmp_path, {"unknown": unknown, "safe": safe})
    first = engine.start(flow(StepDefinition("uncertain", action="unknown", exclusive_resources=("repo:main",))))
    second = engine.start(flow(StepDefinition("safe", action="safe", exclusive_resources=("repo:main",))))
    with ParallelWorkflowRunner(engine, max_workers=2) as runner:
        assert runner.run_until_blocked(first.id).status == "recovery_required"
        assert runner.run_until_blocked(second.id).steps["safe"].status == "pending"
        engine.reconcile_step(first.id, "uncertain", "succeeded", actor="host", evidence="checked remote", output={"ok": True})
        assert runner.run_until_blocked(second.id).status == "completed"
