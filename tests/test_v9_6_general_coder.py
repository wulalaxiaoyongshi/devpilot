"""V9.6：通过真实 Git、V0 工具循环、V4/V5 Job、V7 审批、V8 测试验证通用任务。"""

import json
import subprocess
from pathlib import Path

import pytest

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.runtime.general_plan import checked_python_path, parse_coding_plan, build_general_workflow
from devpilot.runtime.general_coder import GeneralCodingAgent, git


class ScriptModel:
    """只模拟 LLM 回包，真实 Agent、工具、Worker、事件、Git、测试与 Goal 均使用项目原实现。"""

    def __init__(self, role, worker_files, *, planned=None):
        """根据模型角色准备固定 JSON 计划和可追踪的工具调用。"""
        self.role = role
        self.worker_files = worker_files
        self.planned = planned
        self.called = 0

    def complete(self, messages, tools=None, system=None):
        """在分析阶段发起真实工具调用，在 Worker 阶段修改各自授权的 Python 文件。"""
        self.called += 1
        if self.role == "planner":
            return ModelResponse(content=json.dumps(self.planned, ensure_ascii=False))
        if self.role == "lead":
            if self.called == 1:
                return ModelResponse(content="", tool_calls=[ToolCall("a1", "read_file", {"path": "mathbox.py"})])
            return ModelResponse(content="mathbox.add should use +; test_mathbox.py already has a regression test")
        filename, content = self.worker_files[self.role.rsplit("_", 1)[-1]]
        if self.called == 1:
            return ModelResponse(content="", tool_calls=[ToolCall("w1", "write_file", {"path": filename, "content": content})])
        return ModelResponse(content="Updated actual file")


def make_repo(path: Path):
    """创建一个真实 Git 项目，其中数学函数存在可被测试发现的 Bug。"""
    path.mkdir()
    (path / "mathbox.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (path / "test_mathbox.py").write_text("from mathbox import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    for cmd in (("init",), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid"),
                ("add", "-A"), ("commit", "-m", "fixture")):
        subprocess.run(("git", *cmd), cwd=path, capture_output=True, text=True, check=True)


def factory(plan, files):
    """返回按模型身份创建离线响应的工厂，验证旧 Runtime 的真实工具与 Git 接线。"""
    return lambda role: ScriptModel(role, files, planned=plan)


def test_reject_unsafe_and_overlapping_targets(tmp_path):
    """检查模型计划不可使用路径穿越、隐藏配置、相同文件或不受信任的 worker ID。"""
    root = tmp_path / "repo"
    make_repo(root)
    for file in ("../oops.py", ".env.py", ".git/hooks/write.py", "../../etc/passwd", "package/../../x.py"):
        with pytest.raises(ValueError):
            checked_python_path(root, file)
    with pytest.raises(ValueError):
        parse_coding_plan(json.dumps({"summary": "s", "workers": [
            {"id": "one", "task": "a", "files": ["mathbox.py"]},
            {"id": "two", "task": "b", "files": ["mathbox.py"]}]}), root)


def test_plan_guard_requires_approval_for_each_mutating_step(tmp_path):
    """确认动态并行写步骤都依赖 V7 的审批单，而不是仅保护第一个根步骤。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = parse_coding_plan(json.dumps({"summary": "fix math", "workers": [
        {"id": "fix", "task": "fix add", "files": ["mathbox.py"]},
        {"id": "other", "task": "add extra", "files": ["extra.py"]}]}), root)
    definition = build_general_workflow(plan, workflow_id="test_general")
    assert {"fix", "other", "merge", "verify", "delivery_review", "report", "preapproval"} == {s.id for s in definition.steps}
    assert all("preapproval" in s.depends_on for s in definition.steps if s.action == "general.worker")


def test_real_end_to_end_two_approvals_and_goal(tmp_path):
    """验证真实 Worker 写文件、V5 Job、Git 合并、V8 测试和两次人工审批的完整路径。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "修复加法并创建一个新的工具文件", "workers": [
        {"id": "fix", "task": "将 mathbox.add 改为正确加法", "files": ["mathbox.py"]},
        {"id": "other", "task": "创建 extra.py", "files": ["extra.py"]}]}
    edits = {"fix": ("mathbox.py", "def add(a,b):\n    return a + b\n"),
             "other": ("extra.py", "def ok():\n    return True\n")}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, edits)) as app:
        start = app.create("修复加法，并增加一个工具文件")
        run_id = start["run_id"]
        assert start["workflow"] == "waiting_approval"
        assert git(root, "status", "--porcelain") == ""
        approval = start["approvals"][0]
        app.approve(run_id, approval["step"], approval["request_id"], actor="reviewer", approved=True)
        state = app.advance(run_id, timeout_seconds=35)
        assert state["workflow"] == "waiting_approval", state
        assert state["goal"] == "verified", state
        assert (root / "extra.py").read_text().startswith("def ok")
        assert "return a + b" in (root / "mathbox.py").read_text()
        jobs = app._team_app.jobs()
        assert len([job for job in jobs if job.status == "completed"]) == 2
        assert len(app._team_app.core.team.teammates) == 2
        assert not state["report"]
        approval = state["approvals"][0]
        app.approve(run_id, approval["step"], approval["request_id"], actor="reviewer", approved=True)
        final = app.advance(run_id, timeout_seconds=15)
        assert final["workflow"] == "completed", final
        report = json.loads((tmp_path / "state" / "reports" / f"{run_id}.json").read_text(encoding="utf-8"))
        assert report["status"] == "ready_for_delivery" and report["goal"]["goal_status"] == "verified"
        assert set(report["changed_files"]) == {"extra.py", "mathbox.py"}


def test_unapproved_plan_does_not_touch_repo(tmp_path):
    """模型提出写文件后，在用户没有批准的情况下不会启动 Worker 或改变代码。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "hi") })) as app:
        base = git(root, "rev-parse", "HEAD")
        run = app.create("修复测试")
        app.advance(run["run_id"])
        assert git(root, "rev-parse", "HEAD") == base
        assert app._team_app is None
        assert run["goal"] == "pending"


def test_wrong_approval_id_and_actor_denied(tmp_path):
    """拒绝冒用审批人和过期编号，确保审批必须与 V7 当前请求相匹配。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "hi") })) as app:
        run = app.create("修复测试")
        req = run["approvals"][0]
        with pytest.raises(PermissionError):
            app.approve(run["run_id"], req["step"], req["request_id"], actor="worker", approved=True)
        with pytest.raises(ValueError):
            app.approve(run["run_id"], req["step"], "not-a-real-id", actor="reviewer", approved=True)


def test_repeated_session_worker_scoped_branches(tmp_path):
    """不同会话的 Worker Git 分支名称必须不同，避免复用之前的提交。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "hi") })) as app:
        run1 = app.create("修复测试")
        run2 = app.create("再次修复测试")
        assert run1["run_id"] != run2["run_id"]


def test_goal_test_failure_blocks_delivery(tmp_path):
    """Worker 修改了真实文件但未修复 Bug 时，V8 测试失败且不得进入交付审批。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "incorrect fix", "workers": [{"id": "fix", "task": "test change", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "def add(a,b):\n    return a - b\n# modified\n")})) as app:
        start = app.create("修复加法")
        req = start["approvals"][0]
        app.approve(start["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
        status = app.advance(start["run_id"], timeout_seconds=30)
        assert status["workflow"] == "failed", status
        assert status["goal"] != "verified"
        assert status["report"] is None


def test_stale_git_after_verification_blocks_report(tmp_path):
    """实际测试和 Goal 通过后只要源码发生新变化，V8 交付检查就不再认可原证据。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix add", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "def add(a,b):\n    return a+b\n")})) as app:
        start = app.create("修复加法")
        req = start["approvals"][0]
        app.approve(start["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
        status = app.advance(start["run_id"], timeout_seconds=30)
        assert status["goal"] == "verified"
        (root / "mathbox.py").write_text("def add(a,b):\n    return 123\n", encoding="utf-8")
        final_approval = status["approvals"][0]
        app.approve(start["run_id"], final_approval["step"], final_approval["request_id"], actor="reviewer", approved=True)
        finish = app.advance(start["run_id"], timeout_seconds=15)
        assert finish["workflow"] == "failed"
        assert finish["report"] is None


def test_resume_across_app_restart(tmp_path):
    """新进程重新打开 V7、V8 持久化记录后，仍能完成两次审批与交付。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    model_factory = factory(plan, {"fix": ("mathbox.py", "def add(a,b):\n    return a+b\n")})
    state = tmp_path / "state"
    with GeneralCodingAgent(root, state, model_factory) as app:
        first = app.create("修复加法")
        run_id = first["run_id"]
    with GeneralCodingAgent(root, state, model_factory) as app:
        req = app.status(run_id)["approvals"][0]
        app.approve(run_id, req["step"], req["request_id"], actor="reviewer", approved=True)
        second = app.advance(run_id, timeout_seconds=30)
        assert second["workflow"] == "waiting_approval"
    with GeneralCodingAgent(root, state, model_factory) as app:
        req = app.status(run_id)["approvals"][0]
        app.approve(run_id, req["step"], req["request_id"], actor="reviewer", approved=True)
        assert app.advance(run_id, timeout_seconds=10)["workflow"] == "completed"


def test_unittest_without_tests_cannot_be_verified(tmp_path):
    """拒绝将没有发现任何测试的 unittest 空跑当作 Goal 验证成功。"""
    root = tmp_path / "repo"
    make_repo(root)
    (root / "test_mathbox.py").unlink()
    git(root, "add", "-A")
    git(root, "commit", "-m", "drop tests")
    plan = {"summary": "fix", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, {"fix": ("mathbox.py", "def add(a,b):\n    return a+b\n")}), test_mode="unittest") as app:
        first = app.create("修复加法")
        req = first["approvals"][0]
        app.approve(first["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
        result = app.advance(first["run_id"], timeout_seconds=30)
        assert result["workflow"] == "failed"
        assert result["goal"] != "verified"


def test_workers_cannot_write_each_others_files(tmp_path):
    """两个 Worker 即使尝试写对方的文件也不会获得 V1 写权限或交付成功。"""
    root = tmp_path / "repo"
    make_repo(root)
    plan = {"summary": "separate scopes", "workers": [
        {"id": "fix", "task": "edit mathbox", "files": ["mathbox.py"]},
        {"id": "other", "task": "add extra", "files": ["extra.py"]}]}
    swapped = {"fix": ("extra.py", "def unsafe(): return True\n"),
               "other": ("mathbox.py", "def add(a,b):return a+b\n")}
    with GeneralCodingAgent(root, tmp_path / "state", factory(plan, swapped)) as app:
        start = app.create("分别修改两个文件")
        req = start["approvals"][0]
        app.approve(start["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
        state = app.advance(start["run_id"], timeout_seconds=30)
        assert state["workflow"] in {"failed", "recovery_required"}
        assert state["goal"] != "verified"
        assert state["report"] is None
        assert git(root, "status", "--porcelain") == ""


def test_plan_guard_read_dependency_still_requires_mutation_approval():
    """模型把写步骤放在只读步骤后面时仍然必须依赖人工审批。"""
    from devpilot.runtime.plan_guard import PlanActionRule, WorkflowPlanGuard
    plan = {"steps": [
        {"id": "first", "action": "read", "params": {}, "depends_on": []},
        {"id": "second", "action": "write", "params": {}, "depends_on": ["first"]}]}
    guard = WorkflowPlanGuard((PlanActionRule("read", frozenset()), PlanActionRule("write", frozenset(), mutates=True)))
    result = guard.compile(plan, workflow_id="test_guard")
    second = next(step for step in result.steps if step.id == "second")
    assert set(second.depends_on) == {"preapproval", "first"}


def test_cli_real_lifecycle_with_injected_model(tmp_path, monkeypatch, capsys):
    """确认用户可以通过统一 CLI 在不同进程式的调用中创建、审批、推进和查看交付报告。"""
    from devpilot.cli import main
    root = tmp_path / "repo"
    make_repo(root)
    state = tmp_path / "state"
    plan = {"summary": "fix add", "workers": [{"id": "fix", "task": "fix", "files": ["mathbox.py"]}]}
    model_factory = factory(plan, {"fix": ("mathbox.py", "def add(a,b):\n    return a+b\n")})
    common = ["--repo", str(root), "--state", str(state)]
    assert main(["run", *common, "--task", "修复加法"], model_factory=model_factory) == 0
    sessions = list((state / "sessions").glob("*.json"))
    assert len(sessions) == 1
    run_id = sessions[0].stem
    assert main(["status", *common, "--run-id", run_id], model_factory=model_factory) == 0
    status = json.loads((state / "workflows" / f"{run_id}.json").read_text(encoding="utf-8"))
    request_id = status["steps"]["preapproval"]["approval_request"]["request_id"]
    monkeypatch.setattr("builtins.input", lambda prompt: "APPROVE " + request_id)
    assert main(["approve", *common, "--run-id", run_id, "--step", "preapproval",
                 "--request-id", request_id, "--decision", "approve"], model_factory=model_factory) == 0
    assert main(["resume", *common, "--run-id", run_id], model_factory=model_factory) == 0
    status = json.loads((state / "workflows" / f"{run_id}.json").read_text(encoding="utf-8"))
    request_id = status["steps"]["delivery_review"]["approval_request"]["request_id"]
    monkeypatch.setattr("builtins.input", lambda prompt: "APPROVE " + request_id)
    assert main(["approve", *common, "--run-id", run_id, "--step", "delivery_review",
                 "--request-id", request_id, "--decision", "approve"], model_factory=model_factory) == 0
    assert main(["resume", *common, "--run-id", run_id], model_factory=model_factory) == 0
    assert (state / "reports" / f"{run_id}.md").is_file()
