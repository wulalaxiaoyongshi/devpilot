"""使用真正的 V7.1 Engine 验证 V1～V6 适配器与异步 Job 状态边界。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot.workflow import (
    ActionRegistry,
    ExternalOutcome,
    ExternalResolverRegistry,
    StepDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRuntimeAdapters,
    WorkflowStore,
    normalize_mcp_v1_text,
    resolve_arguments,
)


def setup_runtime(tmp_path: Path):
    """每个测试创建独立 Store、ActionRegistry、ExternalResolverRegistry 和真实 WorkflowEngine。"""
    actions = ActionRegistry()
    external = ExternalResolverRegistry()
    adapters = WorkflowRuntimeAdapters(actions, external)
    engine = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external)
    return engine, adapters


def test_v1_tool_routes_through_dispatch_and_resolves_input_and_step(tmp_path):
    """V1 动作只经过注入的 Dispatcher 路径，参数可以引用输入及成功的上游结果。"""
    engine, adapters = setup_runtime(tmp_path)
    seen = []

    def dispatch(name, args):
        """模拟 Dispatcher 执行记录，并返回有明确成功状态的结果。"""
        seen.append((name, args))
        return {"ok": True, "content": {"read": args}}

    adapters.actions.register("analyze", lambda c: {"filename": "README.md"})
    adapters.register_v1_tool(dispatch, allowed_tools={"read_file"})
    flow = WorkflowDefinition("v1", "v1", (
        StepDefinition("analyze", action="analyze"),
        StepDefinition("read", action="v1.tool", depends_on=("analyze",), params={"tool": "read_file", "arguments": {"path": {"$step": {"id": "analyze", "field": "filename"}}, "tag": {"$input": "tag"}}}),
    ))
    run = engine.start(flow, {"tag": "test"})
    result = engine.run_until_blocked(run.id)
    assert result.status == "completed"
    assert seen == [("read_file", {"path": "README.md", "tag": "test"})]
    assert result.steps["read"].output["content"]["read"]["path"] == "README.md"


def test_v1_permission_denial_marks_failed_without_side_effect(tmp_path):
    """Dispatcher 明确报告权限拒绝时 Workflow 步骤失败而不是错误地标记成功。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v1_tool(lambda n, a: {"ok": False, "content": "approval required"}, allowed_tools={"edit_file"})
    flow = WorkflowDefinition("deny", "deny", (StepDefinition("edit", action="v1.tool", params={"tool": "edit_file", "arguments": {}}),))
    run = engine.start(flow)
    assert engine.run_until_blocked(run.id).status == "failed"
    assert "approval required" in engine.store.get(run.id).steps["edit"].error


def test_v1_refuses_unclassified_plain_text(tmp_path):
    """原始 V1 字符串没有明确执行状态时应失败关闭，防止把工具错误当成功。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v1_tool(lambda n, a: "Permission denied", allowed_tools={"write_file"})
    flow = WorkflowDefinition("unknown", "unknown", (StepDefinition("write", action="v1.tool", params={"tool": "write_file"}),))
    assert engine.run_until_blocked(engine.start(flow).id).status == "failed"


def test_v1_allowlist_blocks_undeclared_tool(tmp_path):
    """即使有 Dispatcher，工作流也不能调用宿主未列入白名单的高风险工具。"""
    engine, adapters = setup_runtime(tmp_path)
    called = []
    adapters.register_v1_tool(lambda n, a: called.append(n) or {"ok": True, "content": "ok"}, allowed_tools={"read_file"})
    flow = WorkflowDefinition("blocked", "blocked", (StepDefinition("bad", action="v1.tool", params={"tool": "bash"}),))
    assert engine.run_until_blocked(engine.start(flow).id).status == "failed"
    assert called == []


def test_v3_creates_task_and_passes_id_to_v4(tmp_path):
    """V3 创建 Task 后，V4 的任务分配接收同一 Task ID 并保留 Worker 选择。"""
    engine, adapters = setup_runtime(tmp_path)
    calls = []
    adapters.register_v3_task(lambda payload: calls.append(("create", payload)) or "task-123")
    adapters.register_v4_assignment(lambda task_id, agent_id: calls.append(("assign", task_id, agent_id)) or {"ok": True, "commit": "abc"})
    flow = WorkflowDefinition("team", "team", (
        StepDefinition("create", action="v3.task_create", params={"task": {"description": {"$input": "description"}}}),
        StepDefinition("assign", action="v4.assignment", depends_on=("create",), params={"task_id": {"$step": {"id": "create", "field": "task_id"}}, "agent_id": "coder"}),
    ))
    run = engine.start(flow, {"description": "fix bug"})
    assert engine.run_until_blocked(run.id).status == "completed"
    assert calls == [("create", {"description": "fix bug"}), ("assign", "task-123", "coder")]


def test_v4_assignment_must_confirm_completion(tmp_path):
    """V4 未明确确认任务完成时拒绝继续执行下游步骤。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v4_assignment(lambda task_id, agent_id: {"status": "pending"})
    flow = WorkflowDefinition("v4", "v4", (StepDefinition("run", action="v4.assignment", params={"task_id": "t", "agent_id": "a"}),))
    assert engine.run_until_blocked(engine.start(flow).id).status == "failed"


def test_v5_submit_wait_poll_success_and_follow_up(tmp_path):
    """后台 Job 提交后必须等待明确完成，才允许 V7 执行下游步骤。"""
    engine, adapters = setup_runtime(tmp_path)
    job_state = {"job-1": ExternalOutcome("running")}
    submits = []
    adapters.register_v5_job(lambda kind, payload: submits.append((kind, payload)) or "job-1", lambda job: job_state[job])
    actions = adapters.actions
    actions.register("finalize", lambda ctx: {"seen": ctx.results["build"]["result"]["artifact"]})
    flow = WorkflowDefinition("build", "build", (
        StepDefinition("build", action="v5.job_submit", params={"job_type": "test", "payload": {"name": {"$input": "name"}}}),
        StepDefinition("final", action="finalize", depends_on=("build",)),
    ))
    run = engine.start(flow, {"name": "demo"})
    first = engine.run_until_blocked(run.id)
    assert first.status == "waiting_external"
    assert first.steps["build"].output["reference"] == "job-1"
    assert first.steps["final"].status == "pending"
    assert engine.poll_external(run.id, "build").status == "waiting_external"
    job_state["job-1"] = ExternalOutcome("succeeded", {"artifact": "ok"})
    assert engine.poll_external(run.id, "build").status == "running"
    final = engine.run_until_blocked(run.id)
    assert final.status == "completed"
    assert final.steps["final"].output == {"seen": "ok"}
    assert submits == [("test", {"name": "demo"})]


def test_v5_failed_job_and_explicit_recovery(tmp_path):
    """后台 Job 的失败会停住工作流，不会自动重新提交可能带副作用的任务。"""
    engine, adapters = setup_runtime(tmp_path)
    submissions = []
    status = {"state": "failed"}
    adapters.register_v5_job(lambda kind, payload: submissions.append(kind) or "job-2", lambda job: ExternalOutcome(status["state"], error="process failed"))
    flow = WorkflowDefinition("fail", "fail", (StepDefinition("build", action="v5.job_submit", params={"job_type": "build"}, max_attempts=2),))
    run = engine.start(flow)
    assert engine.run_until_blocked(run.id).status == "waiting_external"
    assert engine.poll_external(run.id, "build").status == "failed"
    assert len(submissions) == 1
    assert engine.run_until_blocked(run.id).status == "failed"
    engine.retry_failed_step(run.id, "build")
    assert engine.run_until_blocked(run.id).status == "waiting_external"
    assert len(submissions) == 2


def test_v5_unknown_result_not_retried(tmp_path):
    """Job 状态未知时保留等待和原始凭据，不自动重试或声称任务成功。"""
    engine, adapters = setup_runtime(tmp_path)
    calls = []
    adapters.register_v5_job(lambda kind, payload: calls.append(kind) or "job-u", lambda job: ExternalOutcome("unknown"))
    flow = WorkflowDefinition("unknown", "unknown", (StepDefinition("step", action="v5.job_submit", params={"job_type": "build"}),))
    run = engine.start(flow)
    engine.run_until_blocked(run.id)
    checked = engine.poll_external(run.id, "step")
    assert checked.status == "waiting_external"
    assert "unknown" in checked.steps["step"].note.lower()
    assert calls == ["build"]


def test_v5_query_error_keeps_pending_ticket(tmp_path):
    """查询 JobStore 暂时失败时保留 waiting_external 状态，避免遗失 Job ID。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v5_job(lambda kind, payload: "job-a", lambda job: (_ for _ in ()).throw(ConnectionError("offline")))
    flow = WorkflowDefinition("offline", "offline", (StepDefinition("step", action="v5.job_submit", params={"job_type": "build"}),))
    run = engine.start(flow)
    engine.run_until_blocked(run.id)
    with pytest.raises(ConnectionError):
        engine.poll_external(run.id, "step")
    assert engine.store.get(run.id).steps["step"].status == "waiting_external"


def test_restart_with_v5_external_state(tmp_path):
    """程序重新创建 Engine 后读取原有 Job ID 并继续轮询，而不是重复提交任务。"""
    engine, adapters = setup_runtime(tmp_path)
    submits = []
    adapters.register_v5_job(lambda kind, payload: submits.append(kind) or "job-r", lambda job: ExternalOutcome("running"))
    flow = WorkflowDefinition("restart", "restart", (StepDefinition("step", action="v5.job_submit", params={"job_type": "build"}),))
    run = engine.start(flow)
    engine.run_until_blocked(run.id)
    actions = ActionRegistry()
    external = ExternalResolverRegistry()
    new = WorkflowRuntimeAdapters(actions, external)
    new.register_v5_job(lambda kind, payload: submits.append(kind) or "job-extra", lambda job: ExternalOutcome("succeeded", {"done": True}))
    engine2 = WorkflowEngine(WorkflowStore(tmp_path / "runs"), actions, external)
    assert engine2.poll_external(run.id, "step").status == "completed"
    assert submits == ["build"]


def test_v6_mcp_prefixed_allowlist_and_success_marker(tmp_path):
    """V6 仅允许指定的完整 MCP 工具名，成功响应必须有 V6.3 的明确标记。"""
    engine, adapters = setup_runtime(tmp_path)
    called = []
    adapters.register_v6_mcp(lambda n, a: called.append((n, a)) or normalize_mcp_v1_text("[MCP RESULT: add; external untrusted data]\n3"), allowed_tools={"mcp__demo__add"})
    flow = WorkflowDefinition("mcp", "mcp", (StepDefinition("tool", action="v6.mcp_tool", params={"tool": "mcp__demo__add", "arguments": {"a": 1, "b": 2}}),))
    assert engine.run_until_blocked(engine.start(flow).id).status == "completed"
    assert called == [("mcp__demo__add", {"a": 1, "b": 2})]


def test_v6_mcp_failure_not_success(tmp_path):
    """MCP 的业务错误、参数错误或审批拒绝不会被记录为步骤成功。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v6_mcp(lambda n, a: normalize_mcp_v1_text("[MCP TOOL ERROR: add; external untrusted data]\nfailed"), allowed_tools={"mcp__demo__add"})
    flow = WorkflowDefinition("mcp-error", "mcp-error", (StepDefinition("tool", action="v6.mcp_tool", params={"tool": "mcp__demo__add"}),))
    assert engine.run_until_blocked(engine.start(flow).id).status == "failed"


def test_no_eval_or_arbitrary_step_reference(tmp_path):
    """输入与上游字段引用严格检查存在性，不执行文本中的 Python 表达式。"""
    ctx = SimpleNamespace(inputs={"x": 1}, results={"s": {"n": 2}})
    with pytest.raises(KeyError):
        resolve_arguments({"$input": "__import__('os').system('rm -rf /')"}, ctx)
    with pytest.raises(KeyError):
        resolve_arguments({"$step": {"id": "other", "field": "n"}}, ctx)
    assert resolve_arguments({"nested": [{"$step": {"id": "s", "field": "n"}}]}, ctx) == {"nested": [2]}


def test_external_resolver_requires_named_provider_and_known_status():
    """外部查询器必须预先注册，且不能返回未知的任意结果结构。"""
    r = ExternalResolverRegistry()
    with pytest.raises(KeyError):
        r.resolve("missing", "j", {})
    r.register("jobs", lambda job, meta: ExternalOutcome("running"))
    assert r.resolve("jobs", "j", {}).status == "running"
    with pytest.raises(ValueError):
        ExternalOutcome("sometime")


def test_v3_runnable_uses_existing_graph_query(tmp_path):
    """Workflow 仅查询 V3 Task Graph 判定的可执行任务，不自行重算依赖关系。"""
    engine, adapters = setup_runtime(tmp_path)
    adapters.register_v3_runnable(lambda: ["task-ready-1", "task-ready-2"])
    flow = WorkflowDefinition("ready", "ready", (StepDefinition("ready", action="v3.runnable"),))
    result = engine.run_until_blocked(engine.start(flow).id)
    assert result.status == "completed"
    assert result.steps["ready"].output == {"task_ids": ["task-ready-1", "task-ready-2"]}
