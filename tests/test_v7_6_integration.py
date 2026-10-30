"""V7.6 用真实 Workflow 模块验证跨阶段状态、人工审批和错误隔离。"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from devpilot.workflow import (
    ExternalOutcome, RetryPolicy, StepDefinition, WorkflowApplication,
    WorkflowBindings, build_coding_workflow, register_coding_verification,
)
from examples.v7_6_full_workflow_demo import run_demo


class RecordingPorts:
    """使用确定性注入记录 V1/V3/V4/V5/V6 调用次数和工作流副作用。"""

    def __init__(self) -> None:
        """准备可观察的任务、模拟外部 Job、拒绝策略和同步记录。"""
        self.calls: list[str] = []
        self.query = ExternalOutcome("running")
        self.disallowed = False
        self.task_count = 0
        self.job_count = 0
        self._lock = threading.Lock()

    def dispatch(self, name: str, args: dict) -> dict:
        """记录 V1 和 MCP 工具调用，并模拟权限拒绝或返回工具内容。"""
        with self._lock:
            self.calls.append(name)
        if self.disallowed and name == "read_file":
            return {"ok": False, "content": "Permission denied"}
        if name == "read_file":
            return {"ok": True, "content": {"text": "feature description"}}
        if name == "write_file":
            return {"ok": True, "content": {"delivered": True}}
        if name == "mcp__docs__search":
            return {"ok": True, "content": {"doc": "example"}}
        return {"ok": False, "content": "Unknown tool"}

    def create_task(self, payload: dict) -> str:
        """模拟 V3 创建 Task，确保不同运行的编号可区分。"""
        with self._lock:
            self.task_count += 1
            return f"task-{self.task_count}"

    def run_assignment(self, task_id: str, worker_id: str) -> dict:
        """模拟 V4 提交明确成功的 Git 分支结果。"""
        with self._lock:
            self.calls.append("worker:" + worker_id)
        return {"ok": True, "result": {"branch": "worker-" + worker_id, "commit": "c01"}}

    def merge(self, ctx) -> dict:
        """检查两个 Worker 已完成并返回汇合证据，防止提前合并。"""
        assert ctx.results["backend"]["result"]["ok"] is True
        assert ctx.results["qa"]["result"]["ok"] is True
        with self._lock:
            self.calls.append("git_merge")
        return {"merged": True, "commits": ["merge_backend", "merge_qa"]}

    def submit(self, job_type: str, payload: dict) -> str:
        """模拟 V5 只提交后台作业而不直接标记它已完成。"""
        with self._lock:
            self.job_count += 1
            self.calls.append("job_submit")
        return "job-01"

    def status(self, job_id: str) -> ExternalOutcome:
        """按测试预设状态反馈同一份外部 Job 的执行结果。"""
        return self.query

    def bindings(self) -> WorkflowBindings:
        """构造有明确工具白名单的 V7.2 回调契约。"""
        return WorkflowBindings(self.dispatch, self.create_task, self.run_assignment,
                                self.submit, self.status, self.merge,
                                frozenset({"read_file", "write_file"}), frozenset({"mcp__docs__search"}))


def setup(tmp_path: Path, ports: RecordingPorts) -> WorkflowApplication:
    """用真实 WorkflowStore 和 ParallelWorkflowRunner 初始化可复用的集成测试宿主。"""
    app = WorkflowApplication(tmp_path / "runs", ports.bindings(), max_workers=3)
    register_coding_verification(app.actions)
    return app


def start_and_wait(app: WorkflowApplication) -> tuple[str, str]:
    """运行到首次后台 Job 等待点并返回当前 Run ID 和状态。"""
    run = app.start(build_coding_workflow(), {"input_file": "README.md", "query": "testing"})
    return run.id, app.run_until_blocked(run.id, timeout_seconds=5).status


def test_full_contract_waits_for_job_and_approval(tmp_path):
    """后台 Job 未完成和审批未通过时必须阻止最终交付。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        run_id, status = start_and_wait(app)
        assert status == "waiting_external"
        assert app.store.get(run_id).steps["deliver"].status == "pending"
        assert ports.job_count == 1
        assert app.tick(run_id, poll_external=True).status == "waiting_external"
        assert ports.job_count == 1
        ports.query = ExternalOutcome("succeeded", {"passed": True})
        app.tick(run_id, poll_external=True)
        waiting = app.run_until_blocked(run_id, timeout_seconds=5)
        assert waiting.status == "waiting_approval"
        assert "write_file" not in ports.calls
        approvals = app.pending_approvals(run_id)
        assert len(approvals) == 1 and approvals[0].approvers == ("reviewer",)
        app.approve(run_id, "review", actor="reviewer", request_id=approvals[0].request_id, approved=True, note="已检查")
        final = app.run_until_blocked(run_id, timeout_seconds=5)
        assert final.status == "completed"
        assert ports.calls.count("write_file") == 1
        assert ports.calls.index("git_merge") < ports.calls.index("job_submit")


def test_denied_dispatch_stops_before_task_creation(tmp_path):
    """V1 拒绝读取工具时不应创建 V3 Task 或向后续步骤推进。"""
    ports = RecordingPorts()
    ports.disallowed = True
    with setup(tmp_path, ports) as app:
        run_id = app.start(build_coding_workflow(), {"input_file": "README.md", "query": "x"}).id
        result = app.run_until_blocked(run_id, timeout_seconds=3)
        assert result.status == "failed"
        assert ports.task_count == 0 and ports.job_count == 0


def test_external_failure_blocks_approval_and_delivery(tmp_path):
    """V5 报告作业失败时，Workflow 不得进入人工交付审批。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        run_id, _ = start_and_wait(app)
        ports.query = ExternalOutcome("failed", error="unittest failed")
        result = app.tick(run_id, poll_external=True)
        assert result.status == "failed"
        assert app.pending_approvals(run_id) == ()
        assert "write_file" not in ports.calls


def test_external_unknown_is_not_resubmitted(tmp_path):
    """外部执行状态未知时只保留原 Job ID，不能自动提交第二次。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        run_id, _ = start_and_wait(app)
        ports.query = ExternalOutcome("unknown")
        for _ in range(3):
            result = app.tick(run_id, poll_external=True)
            assert result.status == "waiting_external"
        assert result.steps["test_job"].output["reference"] == "job-01"
        assert ports.job_count == 1


def test_approval_requires_correct_identity_and_request(tmp_path):
    """错误审批人和伪造请求编号都不能让 Workflow 交付。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        run_id, _ = start_and_wait(app)
        ports.query = ExternalOutcome("succeeded", {"passed": True})
        app.tick(run_id, poll_external=True)
        assert app.run_until_blocked(run_id, timeout_seconds=5).status == "waiting_approval"
        request = app.pending_approvals(run_id)[0]
        with pytest.raises(PermissionError):
            app.approve(run_id, "review", actor="agent", request_id=request.request_id, approved=True)
        with pytest.raises(ValueError):
            app.approve(run_id, "review", actor="reviewer", request_id="stale", approved=True)
        assert "write_file" not in ports.calls


def test_approval_inbox_survives_application_restart(tmp_path):
    """审批单落盘后即使关闭并重建应用也能读取同一请求编号。"""
    ports = RecordingPorts()
    app = setup(tmp_path, ports)
    try:
        run_id, _ = start_and_wait(app)
        ports.query = ExternalOutcome("succeeded", {"passed": True})
        app.tick(run_id, poll_external=True)
        assert app.run_until_blocked(run_id, timeout_seconds=5).status == "waiting_approval"
        old_request = app.pending_approvals(run_id)[0]
    finally:
        app.close()
    with setup(tmp_path, ports) as reopened:
        assert reopened.pending_approvals(run_id) == (old_request,)
        reopened.approve(run_id, "review", actor="reviewer", request_id=old_request.request_id, approved=True)
        assert reopened.run_until_blocked(run_id, timeout_seconds=5).status == "completed"


def test_pending_approvals_scans_all_runs(tmp_path):
    """审批线程可以从同一 WorkflowStore 检索不同 WorkflowRun 的所有待审批单。"""
    ports = RecordingPorts()
    ports.query = ExternalOutcome("succeeded", {"passed": True})
    with setup(tmp_path, ports) as app:
        ids = []
        for _ in range(2):
            run_id, _ = start_and_wait(app)
            app.tick(run_id, poll_external=True)
            app.run_until_blocked(run_id, timeout_seconds=5)
            ids.append(run_id)
        pending = app.pending_approvals()
        assert {x.run_id for x in pending} == set(ids)


def test_recover_does_not_replay_interrupted_action(tmp_path):
    """已经开始但未保存返回结果的动作，在进程重启时不得被再次调用。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        run = app.start(build_coding_workflow(), {"input_file": "README.md", "query": "x"})
        snapshot = app.store.get(run.id)
        snapshot.steps["inspect"].status = "running"
        snapshot.steps["inspect"].attempts = 1
        app.engine._save(snapshot)
        resumed = app.recover(run.id)
        assert resumed.status == "failed"
        assert resumed.steps["inspect"].status == "failed"
        assert ports.calls == []


def test_recovery_marked_required_when_step_has_retry_policy(tmp_path):
    """启用 V7.4 策略的已启动步骤在崩溃后必须先核实，而不是自动重试。"""
    ports = RecordingPorts()
    with setup(tmp_path, ports) as app:
        definition = build_coding_workflow()
        steps = tuple(replace(s, retry=RetryPolicy(idempotent=True), max_attempts=2) if s.id == "inspect" else s for s in definition.steps)
        definition = replace(definition, steps=steps)
        run = app.start(definition, {"input_file": "README.md", "query": "x"})
        snapshot = app.store.get(run.id)
        snapshot.steps["inspect"].status = "running"
        snapshot.steps["inspect"].attempts = 1
        app.engine._save(snapshot)
        recovered = app.recover(run.id)
        assert recovered.status == "recovery_required"
        assert recovered.steps["inspect"].status == "recovery_required"
        assert not ports.calls


def test_application_rejects_bad_bindings(tmp_path):
    """未经明确授权的 MCP 工具命名或空工具白名单不能创建 WorkflowApplication。"""
    ports = RecordingPorts()
    good = ports.bindings()
    with pytest.raises(ValueError):
        replace(good, allowed_mcp_tools=frozenset({"unsafe"}))
    with pytest.raises(ValueError):
        replace(good, allowed_tools=frozenset())


def test_full_demo_with_real_git_merge_and_unittest(tmp_path):
    """实际启动 Git Worktree、执行双分支 Git Merge 和 unittest 后才能生成交付文件。"""
    if not shutil.which("git"):
        pytest.skip("Real Git integration needs git available in PATH")
    run_id, stages = run_demo(tmp_path)
    assert len(run_id) == 32
    assert stages == ["第一阶段：waiting_external", "第二阶段：waiting_approval", "最终：completed"]
    repo = tmp_path / "repo"
    assert (repo / "calculator.py").is_file()
    assert (repo / "tests" / "test_calculator.py").is_file()
    assert "代码已合并" in (repo / "DELIVERED.txt").read_text(encoding="utf-8")


def test_closing_application_rejects_new_ticks(tmp_path):
    """应用关闭以后禁止继续调度，防止新步骤使用已经停止的线程池。"""
    ports = RecordingPorts()
    app = setup(tmp_path, ports)
    run_id = app.start(build_coding_workflow(), {"input_file": "README.md", "query": "x"}).id
    app.close()
    with pytest.raises(RuntimeError):
        app.tick(run_id)
