"""V9.3：验证真实 V0～V5 接线、工作区写入限制、事件幂等和验收报告。"""

import json
from pathlib import Path

import pytest

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.runtime.lead_team import create_lead_team_app
from examples.v9_3_team_online_acceptance import (OfflineTeamModel, create_fixture,
                                                     run_acceptance, write_report, ALLOWED_FILES)


def _make_team(tmp_path: Path, *, dispatch: bool = True):
    """创建独立真实 Git 仓库和最小授权团队供单项测试使用。"""
    root = create_fixture(tmp_path / "repository")
    return create_lead_team_app(root, OfflineTeamModel, state_dir=tmp_path / "state",
                                worker_write_files=ALLOWED_FILES, allow_team_dispatch=dispatch)


def test_offline_end_to_end_v0_to_v5_with_git_and_events(tmp_path):
    """验证任务创建、两个 Worker、真实后台线程、Git Commit、通知与游标实际协同。"""
    result = run_acceptance(tmp_path / "run", OfflineTeamModel, live=False)
    assert result["offline_self_test_passed"] is True
    assert result["passed"] is False
    assert all(result["checks"].values())
    assert set(result["model_calls"]) == {"lead", "backend", "tests"}
    assert len(result["worker_commits"]) == 2
    assert len(result["lead_delivered_event_ids"]) == 2
    assert all(status == "completed" for status in result["job_statuses"].values())
    assert not (tmp_path / "run/fixture_repo/backend_fix.py").exists()


def test_factory_rejects_team_mode_without_explicit_worker_files(tmp_path):
    """禁止创建缺少明确可写文件白名单的团队应用。"""
    repo = create_fixture(tmp_path / "repo")
    with pytest.raises(ValueError):
        create_lead_team_app(repo, OfflineTeamModel)


def test_lead_cannot_call_shell_write_or_change_task_status(tmp_path):
    """Lead 只能规划与派发任务，即使伪造工具名称也无法直接修改仓库。"""
    with _make_team(tmp_path) as app:
        names = app.core.lead.tools.names()
        assert "team_submit_ready" in names and "task_create" in names
        assert "bash" not in names and "write_file" not in names and "task_update" not in names
        for tool in ("bash", "write_file", "edit_file", "task_update", "cron_create", "job_cancel"):
            status = app.core.lead.dispatcher.execute_detailed(ToolCall("x", tool, {}))
            assert status.status == "unknown_tool"
        assert "return 500" in (app.repo_root / "auth.py").read_text(encoding="utf-8")


def test_team_dispatch_requires_explicit_host_authorization(tmp_path):
    """宿主没有授权时，即使模型请求 team_spawn 与 team_submit_ready 也会被权限 Hook 拒绝。"""
    with _make_team(tmp_path, dispatch=False) as app:
        for name, args in (("team_spawn", {"agent_id": "backend", "role": "coder"}),
                           ("team_submit_ready", {"max_assignments": 2})):
            result = app.core.lead.dispatcher.execute_detailed(ToolCall("x", name, args))
            assert result.status == "denied"
        assert not app.teammates()


def test_worker_has_only_bounded_write_and_no_bash(tmp_path):
    """Worker 的工具表不包含 Shell 和任务派发能力，并且不允许写入未批准路径。"""
    with _make_team(tmp_path) as app:
        worker = app.core.create_teammate("backend", "coder")
        names = worker.agent.tools.names()
        assert "write_file" in names and "read_file" in names
        assert not set(names).intersection({"bash", "edit_file", "task_create", "task_update", "team_spawn", "team_submit_ready"})
        for path in ("auth.py", "../evil.py", ".env", ".git/config", "other.py"):
            result = worker.agent.dispatcher.execute_detailed(
                ToolCall("w", "write_file", {"path": path, "content": "print('bad')"}))
            assert result.status == "denied" or result.content.startswith("Error:")
        assert not (worker.worktree.path / "other.py").exists()
        ok = worker.agent.dispatcher.execute_detailed(ToolCall("w", "write_file", {"path": "backend_fix.py", "content": "ANSWER=401\n"}))
        assert ok.status == "returned" and "Wrote" in ok.content
        assert not (app.repo_root / "backend_fix.py").exists()


def test_worker_rejects_oversized_and_non_string_text(tmp_path):
    """超限文件内容和非字符串内容不能被 Worker 写入工作区。"""
    with _make_team(tmp_path) as app:
        worker = app.core.create_teammate("backend", "coder")
        for content in ("a" * 21000, None):
            result = worker.agent.dispatcher.execute(ToolCall("w", "write_file", {"path": "backend_fix.py", "content": content}))
            assert result.startswith("Error:")
        assert not (worker.worktree.path / "backend_fix.py").exists()


def test_worker_symlink_escape_rejected(tmp_path):
    """宿主批准文件名也不能让 Worker 经符号链接写入仓库外部。"""
    with _make_team(tmp_path) as app:
        worker = app.core.create_teammate("backend", "coder")
        outside = tmp_path / "outside.py"
        outside.write_text("SECRET\n", encoding="utf-8")
        try:
            (worker.worktree.path / "backend_fix.py").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("This filesystem does not permit symlinks")
        result = worker.agent.dispatcher.execute(ToolCall("w", "write_file", {"path": "backend_fix.py", "content": "HACK\n"}))
        assert result.startswith("Error:")
        assert outside.read_text(encoding="utf-8") == "SECRET\n"


def test_incorrect_worker_file_path_not_auto_approved(tmp_path):
    """即使工具参数包含不同操作系统的路径分隔符，也必须完全符合文件名白名单。"""
    with _make_team(tmp_path) as app:
        worker = app.core.create_teammate("backend", "coder")
        for path in ("subdir/backend_fix.py", r"..\backend_fix.py", "/tmp/backend_fix.py"):
            result = worker.agent.dispatcher.execute_detailed(ToolCall("x", "write_file", {"path": path, "content": "A=1"}))
            assert result.status == "denied"


def test_report_is_explicitly_offline_not_live(tmp_path):
    """脚本模型跑通时只显示离线自测，不会生成虚假的 DeepSeek 在线 PASS。"""
    output = tmp_path / "artifact"
    result = run_acceptance(output, OfflineTeamModel, live=False)
    write_report(output, result)
    saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
    markdown = (output / "result.md").read_text(encoding="utf-8")
    assert saved["passed"] is False and saved["offline_self_test_passed"]
    assert "OFFLINE_SELF_TEST" in markdown and "真实 DeepSeek 在线通过" in markdown
    assert "DEEPSEEK_API_KEY" not in markdown


def test_event_failure_marks_job_failed_without_false_positive(tmp_path):
    """真实 Worker 运行失败时 V5 应保留 failed 状态，验收不得宣布通过。"""
    class CrashWorker:
        """通过抛异常模拟真实 Worker 的模型异常。"""
        def complete(self, messages, tools=None, system=None):
            """明确抛出执行失败以验证后台 Job 的错误终态。"""
            raise RuntimeError("worker unavailable")

    outcome = run_acceptance(tmp_path / "failure", lambda a: OfflineTeamModel(a) if a == "lead" else CrashWorker(), live=False)
    assert outcome["offline_self_test_passed"] is False
    assert outcome["checks"]["v5_jobs_persisted"] is False
    assert any(status == "failed" for status in outcome["job_statuses"].values())
    assert outcome["checks"]["event_notices_persisted"] is True


def test_v92_analysis_mode_remains_unchanged(tmp_path):
    """新增的团队模式不能改变 V9.2 已有的只读分析入口和白名单。"""
    from devpilot.runtime.lead_analysis import create_lead_analysis_app
    repo = create_fixture(tmp_path / "repo")
    with create_lead_analysis_app(repo, OfflineTeamModel) as app:
        assert "team_spawn" not in app.tools()
        assert "write_file" not in app.tools()
        assert "read_file" in app.tools()


def test_team_spawn_rejects_unapproved_agent_identity(tmp_path):
    """拒绝模型创建未列入宿主白名单的 Worker，避免恶意工作树和分支名称。"""
    with _make_team(tmp_path) as app:
        result = app.core.lead.dispatcher.execute_detailed(ToolCall("x", "team_spawn", {"agent_id": "..", "role": "bad"}))
        assert result.status == "returned" and result.content.startswith("Error:")
        assert not app.teammates()


def test_fake_model_cannot_pass_live_gate(tmp_path):
    """不允许测试脚本通过把 live 参数设成 True 就伪装成真实 DeepSeek 在线调用。"""
    with pytest.raises(TypeError, match="real DeepSeekModel"):
        run_acceptance(tmp_path / "run", OfflineTeamModel, live=True)
