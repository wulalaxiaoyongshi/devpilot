"""V9.4：用真实 Git、V0～V8 组件验证集成契约，同时明确 MCP/LLM 离线替身的边界。"""

from pathlib import Path
import json
import subprocess
from uuid import uuid4

import pytest

from devpilot.core.message import ToolCall
from devpilot.mcp.results import MCPToolResult
from devpilot.runtime.full_chain import V94Integration, build_v9_4_goal, build_v9_4_workflow
from devpilot.runtime.lead_team import create_lead_team_app
from devpilot.tools.base import Tool
from examples.v9_4_full_chain_acceptance import (
    OfflineCodingModel, OfflineIssueBridge, create_fixture, run_acceptance, write_report,
)


def test_real_team_mcp_workflow_goal_and_approval(tmp_path):
    """验证原项目真实团队、Worktree、后台 Job、MCP 适配、Goal 证据和人工审批组成闭环。"""
    result = run_acceptance(tmp_path / "ok", live=False, authorize_fixture_approval=True)
    assert result["offline_self_test_passed"]
    assert not result["passed"]
    assert len(result["worker_job_ids"]) == 2
    assert all(result["checks"].values())
    assert result["goal_status"] == "verified"
    assert result["workflow_status"] == "completed"
    assert result["model_call_count"]["lead"] > 0
    assert Path(result["delivery"]["json_path"]).exists()
    assert Path(result["delivery"]["markdown_path"]).exists()
    repo = tmp_path / "ok" / "fixture_repo"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_real_goal_verified_but_approval_still_required(tmp_path):
    """验证 Goal 验收成功不会绕过 V7 人工审批步骤，也不能自行交付。"""
    result = run_acceptance(tmp_path / "pending", live=False)
    assert result["offline_self_test_passed"]
    assert result["goal_status"] == "verified"
    assert result["workflow_status"] == "waiting_approval"
    assert result["review_approved"] is False
    assert result["delivery"] is None


def test_failure_does_not_create_approval_or_claim_goal_success(tmp_path):
    """模拟 Worker 提交错误修复，确认真实回归测试失败会阻断后续审批。"""
    result = run_acceptance(tmp_path / "bad", live=False, make_wrong_fix=True)
    assert not result["offline_self_test_passed"]
    assert result["workflow_status"] == "failed"
    assert result["goal_status"] != "verified"
    assert result["checks"]["approval_requested"] is False
    assert result["checks"]["goal_criteria_all_pass"] is False


def test_real_mcp_tool_is_denied_without_explicit_lead_permission(tmp_path):
    """确认 V9.3 默认团队权限不会擅自放行新注册的 MCP 工具。"""
    repo = create_fixture(tmp_path / "repo")
    with create_lead_team_app(repo, lambda name: OfflineCodingModel(name),
                              state_dir=tmp_path / "state", worker_write_files=("auth.py",),
                              allow_team_dispatch=True) as app:
        invoked = []
        app.core.lead.tools.register(Tool("mcp__fixture__get_issue", "fixture", {"type": "object"},
                                          lambda args: invoked.append(args) or "should not run"))
        result = app.core.lead.dispatcher.execute_detailed(ToolCall(uuid4().hex, "mcp__fixture__get_issue", {"issue_number": 15}))
        assert result.status == "denied"
        assert invoked == []


def test_allowed_mcp_tool_errors_must_not_be_reported_success(tmp_path):
    """验证 MCP Server 报业务错误时 V7 不会当成已成功取得 Issue。"""
    class ErrorBridge(OfflineIssueBridge):
        """仅在本测试中返回 MCP 业务错误状态。"""
        def call_tool(self, name, arguments):
            """始终返回明确的服务端错误供 V1 适配器处理。"""
            return MCPToolResult(name, True, ("Access denied",), None, ())
    repo = create_fixture(tmp_path / "repo")
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name),
                        mcp_bridge_factory=lambda definition: ErrorBridge()) as app:
        w_id, g_id = app.begin()
        status = app.advance(w_id, timeout_seconds=10)
        assert status == "failed"
        assert app.goal.get(g_id).status == "executing"
        assert app.team.tasks() == []
        assert app.workflow.store.get(w_id).steps["issue"].status == "failed"


def test_rejected_approval_never_completes_workflow(tmp_path):
    """真实验收通过后拒绝审批，工作流必须落盘为 rejected 而不是 completed。"""
    repo = create_fixture(tmp_path / "repo")
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name),
                        mcp_bridge_factory=lambda d: OfflineIssueBridge()) as app:
        w_id, g_id = app.begin()
        assert app.advance(w_id, timeout_seconds=30) == "waiting_approval"
        inbox = app.workflow.pending_approvals(w_id)
        app.workflow.approve(w_id, "approval", actor="reviewer", request_id=inbox[0].request_id,
                             approved=False, note="需要再检查变更")
        assert app.workflow.store.get(w_id).status == "rejected"
        assert app.goal.get(g_id).status == "verified"


def test_bad_actor_cannot_approve(tmp_path):
    """验证传入非授权审批者身份不能越过真正的 V7 审批门禁。"""
    repo = create_fixture(tmp_path / "repo")
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name),
                        mcp_bridge_factory=lambda d: OfflineIssueBridge()) as app:
        w_id, _ = app.begin()
        assert app.advance(w_id, timeout_seconds=30) == "waiting_approval"
        inbox = app.workflow.pending_approvals(w_id)
        with pytest.raises((PermissionError, ValueError)):
            app.workflow.approve(w_id, "approval", actor="attacker", request_id=inbox[0].request_id, approved=True)
        assert app.workflow.store.get(w_id).status == "waiting_approval"


def test_mcp_request_must_be_present_in_true_lead_tool_messages(tmp_path):
    """确认模型要求调用的 MCP 和实际返回给模型的成功工具结果同时存在。"""
    result = run_acceptance(tmp_path / "messages", live=False)
    assert result["checks"]["lead_model_requested_mcp"]
    assert result["checks"]["lead_mcp_tool_result_success"]


def test_workflow_goal_specs_are_independently_validated(tmp_path):
    """检查 V7 蓝图包含人工审批，V8 验收条件真正执行两种登录场景及回归测试。"""
    workflow = build_v9_4_workflow()
    goal = build_v9_4_goal()
    assert workflow.steps[-1].kind == "approval"
    assert len(goal.criteria) == 4
    assert all(criterion.required for criterion in goal.criteria)
    assert any(item.kind == "command_exit_code" for item in goal.criteria)


def test_state_directory_cannot_contaminate_git_evidence(tmp_path):
    """状态目录在仓库内部时直接拒绝，防止工作流 JSON 使 Git 验收证据失效。"""
    repo = create_fixture(tmp_path / "repo")
    with pytest.raises(ValueError):
        V94Integration(repo, repo / ".devpilot", lambda name: OfflineCodingModel(name),
                       mcp_bridge_factory=lambda d: OfflineIssueBridge())


def test_offline_report_never_claims_live_success(tmp_path):
    """确保离线替身的结果明确声明不是在线验收通过。"""
    result = run_acceptance(tmp_path / "fixture", live=False)
    write_report(tmp_path / "fixture", result)
    report = json.loads((tmp_path / "fixture" / "result.json").read_text(encoding="utf-8"))
    assert not report["passed"]
    assert report["offline_self_test_passed"]
    assert "offline" in report["model_mode"]


def test_delivery_report_refuses_changed_git_after_approval(tmp_path):
    """确认验收通过并获批后如果代码被修改，V8 仍会拒绝生成可交付报告。"""
    repo = create_fixture(tmp_path / "repo")
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name),
                        mcp_bridge_factory=lambda d: OfflineIssueBridge()) as app:
        w_id, g_id = app.begin()
        assert app.advance(w_id, timeout_seconds=30) == "waiting_approval"
        inbox = app.workflow.pending_approvals(w_id)[0]
        assert app.approve(w_id, actor="reviewer", request_id=inbox.request_id, approved=True) == "completed"
        (repo / "auth.py").write_text("# unreviewed edit\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not fresh"):
            app.delivery_report(w_id, g_id)


def test_actual_mcp_stdio_server_protocol_when_sdk_available(tmp_path):
    """有官方 MCP v2 SDK 时真实启动独立 stdio Server 并验证 V1 工具链收到其回复。"""
    pytest.importorskip("mcp")
    repo = create_fixture(tmp_path / "repo")
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name)) as app:
        result = app._dispatch_tool("mcp__fixture__get_issue", {"issue_number": 15})
        assert result["ok"] is True
        assert "Issue #15" in result["content"]
