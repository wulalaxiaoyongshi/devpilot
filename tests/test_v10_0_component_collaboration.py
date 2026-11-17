"""V10.0：用真实本地持久化形状验证 V7→V3→V5→V8 证据链。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from devpilot.runtime.component_graph import ComponentGraph
from devpilot.runtime.component_probe import observe_lead


def write(path: Path, data: object) -> None:
    """用正式 JSON 形状创建隔离的本地运行记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def journal(path: Path, rows: list[dict]) -> None:
    """写真实 EventJournal 所使用的逐行事件记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def finished(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """建立一个 Worker 已完成、Event 已 ACK、Goal 已验证的最小闭环。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    run, goal = uuid4().hex, uuid4().hex
    task, job = "task_a1b2c3d4e5", "job_0123456789ab"
    eid = "event_" + job
    write(state / "sessions" / f"{run}.json", {
        "repo": str(repo.resolve()), "run_id": run, "goal_id": goal,
        "plan": {"workers": [{"id": "worker1", "files": ["shop/pricing.py"]}]},
        "v10_probe": {"context_prepares": 2, "memory_recalls": 2,
                      "tools_called": {"read_file": 2, "load_skill": 1, "mcp__github__get_file_contents": 1},
                      "tools_returned": {"read_file": 2, "load_skill": 1, "mcp__github__get_file_contents": 1}},
    })
    write(state / "workflows" / f"{run}.json", {
        "id": run, "status": "completed",
        "steps": {"worker1": {"status": "succeeded", "output": {"task_id": task, "job_id": job, "branch": "devpilot/w_12345678_worker1"}},
                  "verify": {"status": "succeeded", "output": {"status": "verified"}},
                  "report": {"status": "succeeded"}},
    })
    team = state / "team" / run
    write(team / "tasks.json", [{"id": task, "status": "completed", "title": "Fix price"}])
    write(team / "jobs.json", [{"id": job, "status": "completed", "task_id": task}])
    journal(team / "events.jsonl", [
        {"kind": "published", "event": {"id": eid, "type": "job.completed", "task_id": task, "job_id": job}},
        {"kind": "acked", "event_id": eid},
    ])
    write(team / "lead_notices.json", [{"event_id": eid, "job_id": job, "task_id": task,
                                        "event_type": "job.completed", "summary": "ok"}])
    write(team / "lead_cursor.json", {"position": 1})
    write(state / "goal" / "goals" / f"{goal}.json", {"id": goal, "status": "verified", "criteria": {"tests": {"status": "pass"}}})
    write(state / "reports" / f"{run}.json", {"run_id": run, "github_pr": {"url": "https://github.com/a/b/pull/1"}})
    journal(state / "component_audit" / f"{run}.jsonl", [
        {"component": "V0", "event": "lead_analyzed", "meta": {}},
    ])
    return repo, state, run, goal


def inspect(data: tuple) -> dict:
    """复用真正的 ComponentGraph，只读取原生 JSON 和 JSONL。"""
    repo, state, run, _ = data
    return ComponentGraph(repo, state).inspect(run)


def test_full_worker_job_event_lead_goal_chain(finished):
    result = inspect(finished)
    assert result["workflow_status"] == "completed"
    assert result["goal_status"] == "verified"
    assert result["all_worker_chains_linked"] is True
    assert result["findings"] == []
    assert result["worker_chains"][0]["linked"]
    for key in ("task", "background_job", "event_publish", "event_ack", "lead_notice",
                "lead_consumption", "goal_verified", "mcp_tool_call", "memory_recall", "skill_load", "context_prepare"):
        assert result["components"][key]["status"] == "observed", key


def test_does_not_claim_cron_or_mailbox(finished):
    result = inspect(finished)
    assert result["components"]["cron_executed"]["status"] == "not_observed"
    assert result["components"]["mailbox_traffic"]["status"] == "not_observed"


def test_does_not_claim_mcp_when_only_configured(finished):
    repo, state, run, _ = finished
    p = state / "sessions" / f"{run}.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["v10_probe"]["tools_called"].pop("mcp__github__get_file_contents")
    write(p, payload)
    assert inspect(finished)["components"]["mcp_tool_call"]["status"] == "not_observed"


def test_broken_job_task_relation_reported(finished):
    _, state, run, _ = finished
    write(state / "team" / run / "jobs.json", [{"id": "job_0123456789ab", "status": "completed", "task_id": "other"}])
    result = inspect(finished)
    assert result["all_worker_chains_linked"] is False
    assert result["worker_chains"][0]["job_links_task"] is False
    assert "incomplete" in result["findings"][0]


def test_missing_event_ack_reported(finished):
    _, state, run, _ = finished
    p = state / "team" / run / "events.jsonl"
    row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    journal(p, [row])
    result = inspect(finished)
    assert result["worker_chains"][0]["event_acked"] is False
    assert result["all_worker_chains_linked"] is False


def test_missing_notice_reported(finished):
    _, state, run, _ = finished
    write(state / "team" / run / "lead_notices.json", [])
    write(state / "team" / run / "lead_cursor.json", {"position": 0})
    result = inspect(finished)
    assert result["worker_chains"][0]["notice_matches"] is False
    assert result["worker_chains"][0]["lead_consumed"] is False


def test_unconsumed_notice_not_claimed(finished):
    _, state, run, _ = finished
    write(state / "team" / run / "lead_cursor.json", {"position": 0})
    result = inspect(finished)
    assert result["worker_chains"][0]["lead_consumed"] is False


def test_lead_cursor_out_of_bounds(finished):
    _, state, run, _ = finished
    write(state / "team" / run / "lead_cursor.json", {"position": 99})
    assert "Lead cursor exceeds" in inspect(finished)["findings"][0]


def test_goal_mismatch_reported(finished):
    _, state, _, goal = finished
    write(state / "goal" / "goals" / f"{goal}.json", {"id": goal, "status": "executing"})
    result = inspect(finished)
    assert result["components"]["goal_verified"]["status"] == "not_observed"
    assert any("Goal" in x for x in result["findings"])


def test_old_run_without_v10_probe_does_not_fabricate(finished):
    _, state, run, _ = finished
    p = state / "sessions" / f"{run}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("v10_probe")
    write(p, data)
    result = inspect(finished)
    assert result["components"]["context_prepare"]["status"] == "not_observed"
    assert result["components"]["memory_recall"]["status"] == "not_observed"
    assert result["all_worker_chains_linked"] is True


def test_pending_worker_is_not_an_error(finished):
    _, state, run, _ = finished
    write(state / "workflows" / f"{run}.json", {
        "id": run, "status": "waiting_approval",
        "steps": {"worker1": {"status": "pending"}, "verify": {"status": "pending"}},
    })
    result = inspect(finished)
    assert result["worker_chains"] == []
    assert result["all_worker_chains_linked"] is None
    assert result["findings"] == []


def test_invalid_run_id_rejected(finished):
    repo, state, *_ = finished
    with pytest.raises(ValueError):
        ComponentGraph(repo, state).inspect("../secrets")


def test_wrong_repo_rejected(finished, tmp_path):
    _, state, run, _ = finished
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(PermissionError):
        ComponentGraph(other, state).inspect(run)


def test_state_inside_repo_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError):
        ComponentGraph(root, root / ".devpilot")


def test_corrupt_event_log_is_not_silently_ignored(finished):
    _, state, run, _ = finished
    path = state / "team" / run / "events.jsonl"
    path.write_text("{malformed", encoding="utf-8")
    with pytest.raises(ValueError, match="日志"):
        inspect(finished)


def test_graph_is_read_only(finished):
    _, state, run, _ = finished
    before = {str(p): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    inspect(finished)
    after = {str(p): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    assert before == after


class DummyComponent:
    def __init__(self, result: object):
        self.result = result

    def prepare(self, _messages):
        return self.result

    def recall(self, _query, limit=5):
        return "(no relevant memory)"

    def execute(self, call):
        if call.name == "bad":
            raise RuntimeError("error")
        return "ok"


def fake_lead():
    return SimpleNamespace(context=DummyComponent(["prepared"]),
                           memory=DummyComponent("memory"), dispatcher=DummyComponent("dispatched"))


def test_probe_is_real_method_calls_and_no_sensitive_data():
    lead = fake_lead()
    with observe_lead(lead) as p:
        assert lead.context.prepare(["private-prompt"]) == ["prepared"]
        assert lead.memory.recall("secret-input") == "(no relevant memory)"
        assert lead.dispatcher.execute(SimpleNamespace(name="read_file", arguments={"path": ".env"})) == "ok"
        snap = p.snapshot()
    assert snap["context_prepares"] == 1
    assert snap["memory_recalls"] == 1
    assert snap["tools_called"] == {"read_file": 1}
    assert "private-prompt" not in json.dumps(snap)
    assert "secret-input" not in json.dumps(snap)
    assert ".env" not in json.dumps(snap)


def test_probe_restores_methods_after_error():
    lead = fake_lead()
    original = lead.dispatcher.execute
    with pytest.raises(RuntimeError, match="error"):
        with observe_lead(lead) as p:
            lead.dispatcher.execute(SimpleNamespace(name="bad"))
    assert lead.dispatcher.execute.__func__ is original.__func__
    assert p.snapshot()["tools_called"] == {"bad": 1}
    assert p.snapshot()["tools_returned"] == {}


def test_probe_distinguishes_called_from_returned():
    lead = fake_lead()
    with observe_lead(lead) as p:
        lead.dispatcher.execute(SimpleNamespace(name="write_file"))
    assert p.snapshot()["tools_called"] == {"write_file": 1}
    assert p.snapshot()["tools_returned"] == {"write_file": 1}
    assert "success" not in p.snapshot()


def test_cli_components_routes_without_model_or_remote(finished, capsys):
    from devpilot import cli
    repo, state, run, _ = finished
    result = cli.main(["components", "--repo", str(repo), "--state", str(state), "--run-id", run])
    out = capsys.readouterr().out
    assert result == 0
    assert json.loads(out)["all_worker_chains_linked"] is True


def test_cli_components_detects_broken_chain(finished, capsys):
    from devpilot import cli
    repo, state, run, _ = finished
    write(state / "team" / run / "jobs.json", [])
    result = cli.main(["components", "--repo", str(repo), "--state", str(state), "--run-id", run])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["findings"]


def test_chat_components_uses_same_inspector(finished):
    from devpilot.runtime.chat_agent import ChatAgent, ChatStore
    repo, state, run, _ = finished
    store = ChatStore(repo, state)
    data = store.open(new=True)
    data["run_id"] = run
    store.save(data)

    class Driver:
        def components(self, run_id):
            return ComponentGraph(repo, state).inspect(run_id)
    chat = ChatAgent(store, data, Driver(), confirm=lambda _: "")
    reply = chat.reply("/components")
    assert json.loads(reply)["all_worker_chains_linked"] is True


def test_audit_only_records_probe_method_calls(finished):
    from devpilot.runtime.general_coder import GeneralCodingAgent
    repo, state, run, _ = finished
    class Audit:
        def __init__(self): self.events = []
        def record(self, *a, **kw): self.events.append((a, kw))
    app = object.__new__(GeneralCodingAgent)
    app.audit = Audit()
    app._record_component_probe(run, {"component_probe": {
        "context_prepares": 1, "memory_recalls": 1, "tools_called": {"read_file": 2, "load_skill": 1},
    }})
    names = [args[2] for args, _ in app.audit.events]
    assert "context_prepare_invoked" in names
    assert "memory_recall_invoked" in names
    assert "lead_tool_dispatch_invoked" in names
    assert "skill_tool_invoked" in names
    assert "mcp_tool_invoked" not in names


def test_real_v10_coding_pipeline_crosslinks_all_persisted_layers(tmp_path):
    """真实 Git/Worker/Job/Event/Goal 完整执行；只用离线脚本模型代替在线 DeepSeek。"""
    from devpilot.runtime.general_coder import GeneralCodingAgent
    from test_v9_8_unified_integration import fixture as make_git_fixture, make_factory, _approve

    repo, _ = make_git_fixture(tmp_path)
    factory, _ = make_factory(fix=True)
    state = tmp_path / "state"
    with GeneralCodingAgent(repo, state, factory) as app:
        started = app.create("修复 mathbox.add 错误")
        probe = app.session(started["run_id"])["v10_probe"]
        assert probe["context_prepares"] >= 1
        assert probe["memory_recalls"] >= 1
        assert probe["tools_called"].get("read_file", 0) >= 1
        assert probe["tools_called"].get("mcp__github__x", 0) == 0
        _approve(app, started)
        verified = app.advance(started["run_id"], timeout_seconds=45)
        assert verified["goal"] == "verified", verified
        _approve(app, verified)
        final = app.advance(started["run_id"], timeout_seconds=20)
        assert final["workflow"] == "completed"

    proof = ComponentGraph(repo, state).inspect(started["run_id"])
    assert proof["findings"] == [], proof
    assert proof["all_worker_chains_linked"] is True
    assert proof["components"]["context_prepare"]["status"] == "observed"
    assert proof["components"]["memory_recall"]["status"] == "observed"
    assert proof["components"]["tool_dispatch"]["status"] == "observed"
    assert proof["components"]["mcp_tool_call"]["status"] == "not_observed"
    assert proof["components"]["goal_verified"]["status"] == "observed"


def test_repair_delivery_uses_source_workflow_and_fresh_goal_evidence(finished):
    """返工交付没有 verify 步骤时，用关联的原 Worker 流程和已验收 Goal 判断。"""
    repo, state, source_id, goal_id = finished
    delivery_id = uuid4().hex
    data = json.loads((state / "sessions" / f"{source_id}.json").read_text(encoding="utf-8"))
    data.update({"run_id": delivery_id, "source_worker_run_id": source_id, "mode": "repair_delivery"})
    write(state / "sessions" / f"{delivery_id}.json", data)
    write(state / "workflows" / f"{delivery_id}.json", {
        "id": delivery_id, "status": "completed", "steps": {
            "delivery_review": {"status": "succeeded"}, "report": {"status": "succeeded"},
        },
    })
    write(state / "reports" / f"{delivery_id}.json", {"run_id": delivery_id})
    proof = ComponentGraph(repo, state).inspect(delivery_id)
    assert proof["goal_status"] == "verified"
    assert proof["all_worker_chains_linked"] is True
    assert proof["components"]["goal_verified"]["status"] == "observed"
    assert proof["findings"] == []


def test_repair_delivery_rejects_missing_source_worker_workflow(finished):
    repo, state, source_id, _ = finished
    delivery_id = uuid4().hex
    data = json.loads((state / "sessions" / f"{source_id}.json").read_text(encoding="utf-8"))
    data.update({"run_id": delivery_id, "source_worker_run_id": uuid4().hex, "mode": "repair_delivery"})
    write(state / "sessions" / f"{delivery_id}.json", data)
    write(state / "workflows" / f"{delivery_id}.json", {"id": delivery_id, "status": "completed",
                 "steps": {"report": {"status": "succeeded"}}})
    write(state / "reports" / f"{delivery_id}.json", {"run_id": delivery_id})
    proof = ComponentGraph(repo, state).inspect(delivery_id)
    assert proof["components"]["goal_verified"]["status"] == "not_observed"
    assert any("Repair delivery" in item for item in proof["findings"])


def test_v1_dispatcher_structured_denial_and_unknown_tool():
    """V1 不会将未注册或被 Hook 拦截的工具误报为已执行。"""
    from devpilot.core.message import ToolCall
    from devpilot.tools.base import Tool
    from devpilot.tools.hooks import HookManager
    from devpilot.tools.registry import ToolRegistry
    from devpilot.tools.dispatcher import ToolDispatcher
    calls = []
    tools, hooks = ToolRegistry(), HookManager()
    tools.register(Tool("write_file", "writes", {"type": "object"}, lambda args: calls.append(args) or "success"))
    hooks.register_pre_tool_use(lambda call: "Approval required" if call.name == "write_file" else None)
    dispatcher = ToolDispatcher(tools, hooks)
    assert dispatcher.execute_detailed(ToolCall("u", "not_registered", {})).status == "unknown_tool"
    assert dispatcher.execute_detailed(ToolCall("w", "write_file", {"path": "bad.py"})).status == "denied"
    assert calls == []


def test_v1_dispatcher_structured_execution_and_error():
    """真实执行成功和 Handler 抛异常应返回不同的状态，并保持原 execute 兼容。"""
    from devpilot.core.message import ToolCall
    from devpilot.tools.base import Tool
    from devpilot.tools.hooks import HookManager
    from devpilot.tools.registry import ToolRegistry
    from devpilot.tools.dispatcher import ToolDispatcher
    tools, hooks = ToolRegistry(), HookManager()
    tools.register(Tool("read_file", "read", {"type": "object"}, lambda a: "content"))
    def faulty(_):
        raise RuntimeError("broken")
    tools.register(Tool("broken", "raise", {"type": "object"}, faulty))
    dispatcher = ToolDispatcher(tools, hooks)
    assert dispatcher.execute_detailed(ToolCall("1", "read_file", {})).status == "returned"
    assert dispatcher.execute(ToolCall("2", "read_file", {})) == "content"
    assert dispatcher.execute_detailed(ToolCall("3", "broken", {})).status == "error"


def test_v1_default_permission_is_not_unconditionally_allow():
    """验证历史 V1 权限短路漏洞不再出现，未知高风险工具默认拒绝。"""
    from devpilot.core.message import ToolCall
    from devpilot.tools.permissions import PermissionPolicy
    policy = PermissionPolicy(confirmer=lambda _: False)
    assert policy.check(ToolCall("0", "write_file", {"path": "data.py"})) is not None
    assert policy.check(ToolCall("1", "bash", {"command": "rm -rf /"})) is not None
    assert policy.check(ToolCall("2", "unknown_rpc", {})) is not None
    assert policy.check(ToolCall("3", "read_file", {"path": "readme.md"})) is None


def test_v5_integration_policy_requires_explicit_approval():
    """团队启动、后台取消、Cron 写操作不得仅凭 Agent 模型决定执行。"""
    from devpilot.core.message import ToolCall
    from devpilot.runtime.permission import IntegratedPermission
    from devpilot.tools.permissions import PermissionPolicy
    guard = IntegratedPermission(PermissionPolicy(confirmer=lambda _: False), approve=None)
    for command in ("team_spawn", "team_submit_ready", "job_cancel", "cron_create"):
        assert guard.check(ToolCall("p", command, {})) is not None
