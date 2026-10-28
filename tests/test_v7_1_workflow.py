"""通过确定性动作检查 Workflow 的执行、依赖、分支、审批和失败恢复。"""

from dataclasses import replace

import pytest

from devpilot.workflow import (
    ActionRegistry, StepCondition, StepDefinition, WorkflowDefinition,
    WorkflowEngine, WorkflowStore, validate_definition,
)


def runtime(tmp_path):
    """为每项测试创建互不干扰的工作流存储与动作注册表。"""
    store = WorkflowStore(tmp_path / "workflows")
    actions = ActionRegistry()
    return WorkflowEngine(store, actions), actions


def definition(*steps):
    """将若干步骤快捷组装成一份合法的测试工作流定义。"""
    return WorkflowDefinition("demo", "演示工作流", tuple(steps))


def test_serial_dependencies_and_persistence(tmp_path):
    """验证步骤按依赖执行并且重建 Store 后依旧能读取结果。"""
    engine, actions = runtime(tmp_path)
    called = []

    def step_a(ctx):
        """保存输入并返回一个 JSON 结果。"""
        called.append("a")
        return {"value": ctx.inputs["value"]}

    def step_b(ctx):
        """读取上游结果并生成新的步骤结果。"""
        called.append("b")
        return {"double": ctx.results["a"]["value"] * 2}

    actions.register("first", step_a)
    actions.register("second", step_b)
    wf = definition(
        StepDefinition("b", action="second", depends_on=("a",)),
        StepDefinition("a", action="first"),
    )
    run = engine.start(wf, {"value": 7})
    completed = engine.run_until_blocked(run.id)
    assert completed.status == "completed"
    assert called == ["a", "b"]
    assert completed.steps["b"].output == {"double": 14}
    reopened = WorkflowStore(tmp_path / "workflows").get(run.id)
    assert reopened.steps["a"].output == {"value": 7}
    assert reopened.definition.steps[0].depends_on == ("a",)
    assert len(WorkflowStore(tmp_path / "workflows").list_runs()) == 1


def test_branching_and_join(tmp_path):
    """验证只执行条件匹配的分支，并让汇合步骤允许另一条分支被跳过。"""
    engine, actions = runtime(tmp_path)
    called = []
    actions.register("choose", lambda ctx: {"mode": ctx.inputs["mode"]})

    def record(ctx):
        """记录本轮实际执行的业务分支。"""
        called.append(ctx.step_id)
        return {"branch": ctx.step_id}

    actions.register("record", record)
    wf = definition(
        StepDefinition("choose", action="choose"),
        StepDefinition("edit", action="record", depends_on=("choose",), condition=StepCondition("choose", "mode", "edit")),
        StepDefinition("explain", action="record", depends_on=("choose",), condition=StepCondition("choose", "mode", "explain")),
        StepDefinition("join", action="record", depends_on=("edit", "explain"), allow_skipped_dependencies=("edit", "explain")),
    )
    run = engine.start(wf, {"mode": "edit"})
    result = engine.run_until_blocked(run.id)
    assert result.status == "completed"
    assert result.steps["edit"].status == "succeeded"
    assert result.steps["explain"].status == "skipped"
    assert result.steps["join"].status == "succeeded"
    assert called == ["edit", "join"]


def test_skipped_dependency_propagates(tmp_path):
    """验证依赖步骤被跳过后，下游不会错误执行。"""
    engine, actions = runtime(tmp_path)
    actions.register("choose", lambda ctx: {"mode": "skip"})
    invoked = []
    actions.register("touch", lambda ctx: invoked.append(ctx.step_id) or {"ok": True})
    wf = definition(
        StepDefinition("choose", action="choose"),
        StepDefinition("edit", action="touch", depends_on=("choose",), condition=StepCondition("choose", "mode", "edit")),
        StepDefinition("test", action="touch", depends_on=("edit",)),
    )
    result = engine.run_until_blocked(engine.start(wf).id)
    assert result.status == "completed"
    assert result.steps["edit"].status == "skipped"
    assert result.steps["test"].status == "skipped"
    assert invoked == []


def test_approval_pause_resume(tmp_path):
    """验证审批节点暂停流程，批准后才执行下一步。"""
    engine, actions = runtime(tmp_path)
    history = []
    actions.register("act", lambda ctx: history.append(ctx.step_id) or {"ok": True})
    wf = definition(
        StepDefinition("prepare", action="act"),
        StepDefinition("review", kind="approval", depends_on=("prepare",)),
        StepDefinition("publish", action="act", depends_on=("review",)),
    )
    run = engine.start(wf)
    waiting = engine.run_until_blocked(run.id)
    assert waiting.status == "waiting_approval"
    assert waiting.steps["publish"].status == "pending"
    assert history == ["prepare"]
    approved = engine.approve_step(run.id, "review", True, "已审核变更")
    assert approved.steps["review"].output == {"approved": True, "note": "已审核变更"}
    result = engine.run_until_blocked(run.id)
    assert result.status == "completed"
    assert history == ["prepare", "publish"]


def test_approval_rejected_stops_execution(tmp_path):
    """验证人工拒绝后流程终止，后续动作不会执行。"""
    engine, actions = runtime(tmp_path)
    actions.register("danger", lambda ctx: pytest.fail("不应执行被拒绝的动作"))
    wf = definition(
        StepDefinition("gate", kind="approval"),
        StepDefinition("dangerous", action="danger", depends_on=("gate",)),
    )
    run = engine.start(wf)
    assert engine.run_until_blocked(run.id).status == "waiting_approval"
    assert engine.approve_step(run.id, "gate", False, "未获得批准").status == "rejected"
    assert engine.run_until_blocked(run.id).steps["dangerous"].status == "pending"
    with pytest.raises(ValueError):
        engine.approve_step(run.id, "gate", True)


def test_failure_explicit_retry(tmp_path):
    """验证步骤失败后先暂停，只有明确批准重试才会再次执行。"""
    engine, actions = runtime(tmp_path)
    counts = {"calls": 0}

    def flaky(ctx):
        """第一次尝试主动报错，第二次尝试成功返回。"""
        counts["calls"] += 1
        if counts["calls"] == 1:
            raise RuntimeError("temporary problem")
        return {"ok": True, "attempt": ctx.attempt}

    actions.register("flaky", flaky)
    run = engine.start(definition(StepDefinition("test", action="flaky", max_attempts=2)))
    assert engine.run_until_blocked(run.id).status == "failed"
    assert counts["calls"] == 1
    assert engine.run_until_blocked(run.id).status == "failed"
    engine.retry_failed_step(run.id, "test")
    done = engine.run_until_blocked(run.id)
    assert done.status == "completed"
    assert done.steps["test"].attempts == 2
    assert done.steps["test"].output == {"ok": True, "attempt": 2}


def test_retry_attempt_limit(tmp_path):
    """验证超过最大尝试次数后不能继续重放可能产生副作用的动作。"""
    engine, actions = runtime(tmp_path)
    actions.register("fail", lambda ctx: 1 / 0)
    run = engine.start(definition(StepDefinition("fail_step", action="fail", max_attempts=1)))
    assert engine.run_until_blocked(run.id).status == "failed"
    with pytest.raises(ValueError, match="Maximum attempts"):
        engine.retry_failed_step(run.id, "fail_step")


def test_unregistered_action_is_recorded_as_failure(tmp_path):
    """验证未注册动作会作为步骤失败记录，而不会导致整个 Engine 异常退出。"""
    engine, _ = runtime(tmp_path)
    run = engine.start(definition(StepDefinition("work", action="missing")))
    final = engine.run_until_blocked(run.id)
    assert final.status == "failed"
    assert "not registered" in final.steps["work"].error


def test_non_json_action_result_becomes_failure(tmp_path):
    """验证动作返回集合等非 JSON 数据时会报错并保留失败状态。"""
    engine, actions = runtime(tmp_path)
    actions.register("invalid", lambda ctx: {"bad": {1, 2}})
    run = engine.start(definition(StepDefinition("invalid", action="invalid")))
    result = engine.run_until_blocked(run.id)
    assert result.status == "failed"
    assert "TypeError" in result.steps["invalid"].error


def test_cycle_and_unknown_dependency_rejected(tmp_path):
    """验证循环依赖和不存在的步骤引用在创建运行记录前就被拒绝。"""
    cyclic = definition(
        StepDefinition("a", action="a", depends_on=("b",)),
        StepDefinition("b", action="b", depends_on=("a",)),
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_definition(cyclic)
    with pytest.raises(ValueError, match="Unknown dependency"):
        validate_definition(definition(StepDefinition("a", action="a", depends_on=("missing",))))
    with pytest.raises(ValueError, match="direct dependency"):
        validate_definition(definition(StepDefinition("a", action="a", condition=StepCondition("b", "flag", True))))


def test_duplicate_steps_and_optional_edges_rejected(tmp_path):
    """验证重复步骤和不属于依赖关系的可跳过引用不能创建。"""
    with pytest.raises(ValueError, match="Duplicated step"):
        validate_definition(definition(StepDefinition("a", action="a"), StepDefinition("a", action="b")))
    with pytest.raises(ValueError, match="Skipped dependencies"):
        validate_definition(definition(StepDefinition("a", action="a", allow_skipped_dependencies=("x",))))
    with pytest.raises(ValueError, match="must not"):
        validate_definition(definition(StepDefinition("review", kind="approval", action="wrong")))


def test_interrupted_step_requires_manual_recovery(tmp_path):
    """验证重启后 running 步骤被标记为失败而不自动重跑。"""
    engine, actions = runtime(tmp_path)
    called = []
    actions.register("write", lambda ctx: called.append(1) or {"done": True})
    run = engine.start(definition(StepDefinition("upload", action="write", max_attempts=2)))
    saved = engine.store.get(run.id)
    saved.steps["upload"].status = "running"
    saved.steps["upload"].attempts = 1
    engine.store.save(saved)
    reopened = WorkflowEngine(WorkflowStore(tmp_path / "workflows"), actions)
    recovered = reopened.recover_interrupted(run.id)
    assert recovered.status == "failed"
    assert "side effect may have completed" in recovered.steps["upload"].error
    assert called == []
    reopened.retry_failed_step(run.id, "upload")
    assert reopened.run_until_blocked(run.id).status == "completed"
    assert called == [1]


def test_step_limit_allows_bounded_progress(tmp_path):
    """验证长流程可以每轮只推进有限步骤并保留中间状态。"""
    engine, actions = runtime(tmp_path)
    actions.register("noop", lambda ctx: {"ok": True})
    wf = definition(*(
        StepDefinition(f"step{i}", action="noop", depends_on=(f"step{i-1}",) if i else ())
        for i in range(5)
    ))
    run = engine.start(wf)
    partial = engine.run_until_blocked(run.id, max_transitions=2)
    assert partial.status == "running"
    assert [v.status for v in partial.steps.values()].count("succeeded") == 2
    assert engine.run_until_blocked(run.id).status == "completed"
    with pytest.raises(ValueError):
        engine.run_until_blocked(run.id, max_transitions=0)


def test_store_path_validation_and_action_registry(tmp_path):
    """验证运行记录的路径安全以及动作注册表不允许覆盖同名动作。"""
    engine, actions = runtime(tmp_path)
    with pytest.raises(ValueError):
        engine.store.get("../../secrets")
    actions.register("noop", lambda ctx: {})
    assert actions.names() == ("noop",)
    with pytest.raises(ValueError, match="already registered"):
        actions.register("noop", lambda ctx: {})
    with pytest.raises(KeyError):
        actions.get("unknown")


def test_input_and_action_parameters_cannot_mutate_saved_state(tmp_path):
    """验证动作修改收到的参数副本时不会污染磁盘中的工作流定义或输入。"""
    engine, actions = runtime(tmp_path)

    def mutate(ctx):
        """故意修改收到的输入副本并返回结果，检查是否影响持久化值。"""
        ctx.inputs["settings"]["mode"] = "changed"
        ctx.params["nested"]["x"] = 9
        return {"ok": True}

    actions.register("mutate", mutate)
    wf = definition(StepDefinition("mutate", action="mutate", params={"nested": {"x": 1}}))
    input_data = {"settings": {"mode": "original"}}
    run = engine.start(wf, input_data)
    input_data["settings"]["mode"] = "external mutation"
    assert engine.run_until_blocked(run.id).status == "completed"
    persisted = engine.store.get(run.id)
    assert persisted.inputs["settings"]["mode"] == "original"
    assert persisted.definition.steps[0].params["nested"]["x"] == 1
