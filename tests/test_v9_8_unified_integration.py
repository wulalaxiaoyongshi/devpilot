"""V9.8：使用真实 Git、V7/V8/Worker 离线测试动态编排、PR 发布和受控返工。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.runtime.component_audit import ComponentAudit
from devpilot.runtime.general_coder import GeneralCodingAgent, git
from devpilot.runtime.general_plan import build_general_workflow, parse_coding_plan
from devpilot.runtime.workflow_recipes import (analysis_workflow, with_remote_delivery,
                                                repair_workflow, repair_delivery_workflow)
from devpilot.runtime import pr_publisher
from devpilot.workflow.engine import validate_definition


class ScriptModel:
    """为离线测试提供确定模型响应，真实 Agent Loop、工具、Git、Job 与 Goal 不使用替身。"""

    def __init__(self, role: str, state: dict):
        """保存模型身份和是否修复 Bug 的共享开关。"""
        self.role = role
        self.state = state
        self.calls = 0

    def complete(self, messages, tools=None, system=None):
        """让实际 Agent 工具修改仓库文件，并按测试情形返回正确或错误代码。"""
        self.calls += 1
        if self.role == "planner":
            return ModelResponse(content=json.dumps(self.state["plan"], ensure_ascii=False))
        if self.role == "lead":
            if self.calls == 1:
                return ModelResponse(content="", tool_calls=[ToolCall("read1", "read_file", {"path": "mathbox.py"})])
            return ModelResponse(content="Checked mathbox.py and its tests")
        if self.calls == 1:
            code = "def add(a, b):\n    return a + b\n" if self.state["fix"] else "def add(a, b):\n    return a - b\n# Changed but still broken\n"
            return ModelResponse(content="", tool_calls=[ToolCall("write1", "write_file", {"path": "mathbox.py", "content": code})])
        return ModelResponse(content="Updated code via the actual write_file tool")


def _git(path: Path, *argv: str) -> str:
    """在测试中以独立 Git 参数列表创建真实提交与裸远端。"""
    result = subprocess.run(["git", *argv], cwd=path, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


def fixture(tmp_path: Path, *, remote: bool = False):
    """创建初始 pytest 会失败的 Python Git 项目，可选绑定本机裸 Git 远端。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathbox.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_mathbox.py").write_text("from mathbox import add\n\ndef test_add():\n    assert add(2,3) == 5\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "DevPilot Tester")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "switch", "-C", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial bug")
    bare = None
    if remote:
        bare = tmp_path / "remote.git"
        bare.mkdir()
        _git(bare, "init", "--bare")
        _git(root, "remote", "add", "origin", str(bare))
        _git(root, "push", "-u", "origin", "main")
    return root, bare


def make_factory(*, fix: bool = True):
    """建立可在返工前切换输出的测试模型工厂。"""
    state = {"fix": fix, "plan": {"summary": "fix bug", "workers": [
        {"id": "fix", "task": "repair add method", "files": ["mathbox.py"]}]}}
    return lambda role: ScriptModel(role, state), state


def _approve(app: GeneralCodingAgent, result: dict):
    """在离线安全测试中模拟人类按当前 V7 request_id 批准。"""
    assert len(result["approvals"]) == 1, result
    request = result["approvals"][0]
    return app.approve(result["run_id"], request["step"], request["request_id"], actor="reviewer", approved=True)


def test_workflow_recipes_are_valid_and_gate_pr(tmp_path):
    """动态 Workflow 只能在已有 V8 交付报告后进行 PR 审批和发布。"""
    repo, _ = fixture(tmp_path)
    plan = parse_coding_plan(json.dumps({"summary": "fix", "workers": [
        {"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}), repo)
    local = build_general_workflow(plan, workflow_id="local_fix")
    remote = with_remote_delivery(local, approver="reviewer")
    assert [step.id for step in remote.steps][-3:] == ["report", "publish_review", "publish_pr"]
    assert remote.steps[-1].depends_on == ("publish_review",)
    assert any(step.id == "preapproval" for step in repair_workflow(local).steps)
    assert [step.id for step in repair_delivery_workflow("repair_final", approver="reviewer", publish=True).steps] == ["delivery_review", "report", "publish_review", "publish_pr"]
    assert [step.id for step in analysis_workflow("analysis_1").steps] == ["analysis_report"]
    validate_definition(remote)


def test_readonly_analysis_runs_without_goal_or_worker(tmp_path):
    """只读分析不创建 Worker 和 Goal，也不会更改 Git 提交。"""
    repo, _ = fixture(tmp_path)
    factory, _ = make_factory()
    baseline = git(repo, "rev-parse", "HEAD")
    with GeneralCodingAgent(repo, tmp_path / "state", factory) as app:
        run = app.create("请分析这个项目的 add Bug", mode="analysis")
        assert run["goal"] == "not_applicable"
        end = app.advance(run["run_id"])
        assert end["workflow"] == "completed"
        assert end["report"] and Path(end["report"]).is_file()
        assert app._team_app is None
        assert git(repo, "rev-parse", "HEAD") == baseline
        assert "V7" in app.audit.summary(run["run_id"])["observed_components"]


def test_pr_mode_requires_issue_origin_and_trust(tmp_path):
    """没有明确 Issue URL、没有正确 Git origin 或分析任务请求发布均会被拒绝。"""
    repo, _ = fixture(tmp_path)
    factory, _ = make_factory()
    issue = "https://github.com/demo/lab/issues/1"
    with GeneralCodingAgent(repo, tmp_path / "state", factory) as app:
        with pytest.raises(ValueError):
            app.create("修复 Bug", delivery="github-pr")
        with pytest.raises(PermissionError):
            app.create("修复 " + issue, delivery="github-pr", issue_url=issue)
        with pytest.raises(ValueError):
            app.create("analysis", mode="analysis", delivery="github-pr", issue_url=issue)


def test_integrated_pr_needs_third_approval_and_never_pushes_main(tmp_path, monkeypatch):
    """真实 Worker/Goal 通过后只有第三次审批才推送修复分支，裸远端 main 不变。"""
    repo, bare = fixture(tmp_path, remote=True)
    factory, _ = make_factory()
    original_main = _git(repo, "rev-parse", "main")
    issue = "https://github.com/demo/lab/issues/9"
    monkeypatch.setattr(pr_publisher, "_assert_origin", lambda *_: None)
    prs = []

    def fake_gh(_repo, *args):
        """模拟 GitHub PR 元数据 API，远端 Git push 仍走真实裸 Git 仓库。"""
        if args[:2] == ("pr", "list"):
            return json.dumps(prs)
        if args[:2] == ("pr", "create"):
            branch = args[args.index("--head") + 1]
            prs.append({"url": "https://github.com/demo/lab/pull/1", "state": "OPEN",
                        "baseRefName": "main", "headRefName": branch, "headRefOid": _git(repo, "rev-parse", "HEAD")})
            return prs[-1]["url"]
        raise AssertionError("Unexpected GitHub CLI call")

    monkeypatch.setattr(pr_publisher, "_gh", fake_gh)
    with GeneralCodingAgent(repo, tmp_path / "state", factory) as app:
        started = app.create("修复 " + issue, delivery="github-pr", issue_url=issue)
        _approve(app, started)
        testing = app.advance(started["run_id"], timeout_seconds=45)
        assert testing["goal"] == "verified", testing
        assert testing["approvals"][0]["step"] == "delivery_review"
        _approve(app, testing)
        before_publish = app.advance(started["run_id"], timeout_seconds=20)
        assert before_publish["workflow"] == "waiting_approval", before_publish
        assert before_publish["approvals"][0]["step"] == "publish_review"
        assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == original_main
        assert prs == []
        _approve(app, before_publish)
        published = app.advance(started["run_id"], timeout_seconds=40)
        assert published["workflow"] == "completed", published
        assert len(prs) == 1 and prs[0]["state"] == "OPEN"
        assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == original_main
        assert app.audit.summary(started["run_id"])["events_count"] >= 7


def test_v8_repair_loop_generates_new_approved_workflow(tmp_path):
    """首次失败后 V8 生成 Feedback/RepairTicket，第二次真实 Worker 修复且重新验收并交付。"""
    repo, _ = fixture(tmp_path)
    factory, state = make_factory(fix=False)
    save = tmp_path / "state"
    with GeneralCodingAgent(repo, save, factory) as app:
        first = app.create("修复计算错误")
        _approve(app, first)
        failed = app.advance(first["run_id"], timeout_seconds=45)
        assert failed["workflow"] == "failed", failed
        assert failed["goal"] == "verifying"
        assert not failed["report"]
    state["fix"] = True
    with GeneralCodingAgent(repo, save, factory) as app:
        ticket = app.prepare_repair(first["run_id"])
        assert ticket["round"] == 1 and ticket["status"] == "prepared"
        repair = app.dispatch_repair(first["run_id"], ticket["ticket_id"])
        assert repair["workflow"] == "waiting_approval", repair
        _approve(app, repair)
        repaired = app.advance(repair["run_id"], timeout_seconds=45)
        assert repaired["workflow"] == "completed", repaired
        assert repaired["goal"] == "verified", repaired
        assert "next_run_id" in repaired
        delivery_id = repaired["next_run_id"]
        review = app.status(delivery_id)
        assert review["approvals"][0]["step"] == "delivery_review"
        _approve(app, review)
        result = app.advance(delivery_id)
        assert result["workflow"] == "completed" and result["report"]
        assert (repo / "mathbox.py").read_text(encoding="utf-8").find("a + b") > 0


def test_component_audit_does_not_invent_unobserved_capabilities(tmp_path):
    """审计只能记录已触发组件，不会虚报 Memory、Skills、Cron 或 Context 压缩。"""
    logger = ComponentAudit(tmp_path / "state")
    run_id = "a" * 32
    logger.record(run_id, "V7", "workflow_started", mode="analysis")
    data = logger.summary(run_id)
    assert data["observed_components"] == ["V7"]
    assert "V3" in data["not_observed_components"]
    assert "context_compaction" in data["not_proven_by_this_audit"]
    with pytest.raises(ValueError):
        logger.record("../unsafe", "V7", "workflow_started")


def test_restricted_write_scope_rejects_model_extra_files(tmp_path):
    """即使模型计划格式正确，超出用户允许的文件范围也不能进入 Workflow。"""
    repo, _ = fixture(tmp_path)
    factory, state = make_factory()
    state["plan"]["workers"][0]["files"] = ["test_mathbox.py"]
    with GeneralCodingAgent(repo, tmp_path / "state", factory, allowed_files=("mathbox.py",)) as app:
        with pytest.raises(PermissionError, match="scope"):
            app.create("修复代码", mode="edit")
        assert list((tmp_path / "state" / "sessions").glob("*.json")) == [] if (tmp_path / "state" / "sessions").exists() else True


def test_v8_failing_test_prepares_ticket_before_manual_dispatch(tmp_path):
    """Goal 测试失败会自动保存 V8 返工意图，但在人工审批前不再次修改代码。"""
    repo, _ = fixture(tmp_path)
    factory, _ = make_factory(fix=False)
    with GeneralCodingAgent(repo, tmp_path / "state", factory) as app:
        start = app.create("修复计算错误")
        _approve(app, start)
        failure = app.advance(start["run_id"], timeout_seconds=45)
        assert failure["workflow"] == "failed" and failure["goal"] == "verifying"
        goal_id = app.session(start["run_id"])["goal_id"]
        tickets = app.goal.repair_store.list_for_goal(goal_id)
        assert len(tickets) == 1 and tickets[0].status == "prepared"
        before = git(repo, "rev-parse", "HEAD")
        assert app.prepare_repair(start["run_id"])["ticket_id"] == tickets[0].id
        assert git(repo, "rev-parse", "HEAD") == before


def test_v9_8_live_verifier_requires_real_model_trace_and_pr(tmp_path):
    """即使本地报告伪称完成，没有实际模型轨迹和 GitHub PR 也不能冒充在线验收。"""
    from examples.v9_8_acceptance import verify_live
    repo, _ = fixture(tmp_path)
    state = tmp_path / "state"
    for folder in ("sessions", "workflows", "reports"):
        (state / folder).mkdir(parents=True)
    run_id = "b" * 32
    issue = "https://github.com/demo/lab/issues/2"
    (state / "sessions" / f"{run_id}.json").write_text(json.dumps({"issue_url": issue, "repo": str(repo)}))
    (state / "workflows" / f"{run_id}.json").write_text(json.dumps({"status": "completed", "steps": {}}))
    (state / "reports" / f"{run_id}.json").write_text(json.dumps({"goal": {"goal_status": "verified", "delivery_status": "ready_for_delivery"}}))
    result = verify_live(repo, state, run_id, issue, tmp_path / "does_not_exist.jsonl")
    assert result["online_passed"] is False
    assert result["checks"]["issue_mcp_read"] is False
    assert result["checks"]["deepseek_api_responses"] is False
    assert result["checks"]["github_pr_exists"] is False


def test_publish_network_uncertainty_never_silently_retries(tmp_path, monkeypatch):
    """GitHub PR 创建响应丢失后 V7 应进入 recovery_required，不得重复远程写入。"""
    repo, bare = fixture(tmp_path, remote=True)
    factory, _ = make_factory()
    issue = "https://github.com/demo/lab/issues/10"
    monkeypatch.setattr(pr_publisher, "_assert_origin", lambda *_: None)
    writes = {"create": 0}

    def broken_gh(_repo, *args):
        """模拟 PR 创建时网络断开，保留真实 Git 分支推送。"""
        if args[:2] == ("pr", "list"):
            return "[]"
        if args[:2] == ("pr", "create"):
            writes["create"] += 1
            raise RuntimeError("Connection was lost after request")
        raise AssertionError("Unknown GitHub action")

    monkeypatch.setattr(pr_publisher, "_gh", broken_gh)
    with GeneralCodingAgent(repo, tmp_path / "state", factory) as app:
        start = app.create("修复 " + issue, delivery="github-pr", issue_url=issue)
        _approve(app, start)
        review = app.advance(start["run_id"], timeout_seconds=50)
        _approve(app, review)
        publish_review = app.advance(start["run_id"], timeout_seconds=20)
        _approve(app, publish_review)
        blocked = app.advance(start["run_id"], timeout_seconds=40)
        assert blocked["workflow"] == "recovery_required", blocked
        assert writes["create"] == 1
        branch = f"devpilot/issue-10-{start['run_id'][:8]}"
        assert _git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
        still_blocked = app.advance(start["run_id"], timeout_seconds=10)
        assert still_blocked["workflow"] == "recovery_required"
        assert writes["create"] == 1
