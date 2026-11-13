"""V9.7：验证最终多场景验收器不会将模拟运行或故障误判为在线成功。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devpilot.cli import main as cli_main
from devpilot.runtime.acceptance import build_final_report, case_passed, safe_message, write_final_report
from devpilot.runtime.diagnostics import environment_report
from examples.v9_7_final_acceptance import make_git_fixture, run_coding_case, run_suite, scenarios


@pytest.fixture(scope="module")
def offline_matrix(tmp_path_factory):
    """只运行一次真实 Git、V4/V5、V7/V8 的四场景离线验收，供各项断言共享。"""
    folder = tmp_path_factory.mktemp("v9_7_matrix") / "acceptance"
    report = run_suite(folder, live=False, authorize_fixture_approval=True)
    return folder, report


def test_offline_matrix_has_four_independent_cases(offline_matrix):
    """检查离线验收必须包含代码分析、修复、功能开发和故障防护四组场景。"""
    _, report = offline_matrix
    assert {row["name"] for row in report["cases"]} == {"analysis", "bugfix", "feature", "failure_guards"}
    assert all(case_passed(row) for row in report["cases"])


def test_offline_success_never_claims_real_deepseek(offline_matrix):
    """即使离线模型完成全部 Git 和测试链路，也不能设置在线验收通过。"""
    _, report = offline_matrix
    assert report["offline_passed"] is True
    assert report["online_passed"] is False
    assert report["mode"] == "OFFLINE_SCRIPT_MODEL"


def test_analysis_mode_did_not_write_files(offline_matrix):
    """验证纯分析模式读到了真实源码，但不具备写文件或 Shell 工具权限。"""
    _, report = offline_matrix
    case = next(x for x in report["cases"] if x["name"] == "analysis")
    assert case["checks"]["lead_received_source"] is True
    assert case["checks"]["repository_unchanged"] is True
    assert case["checks"]["forbidden_tools_absent"] is True


def test_bugfix_has_real_git_and_goal_delivery(offline_matrix):
    """验证数学 Bug 由真实 Worker 修改、pytest 验收后才获得可交付报告。"""
    directory, report = offline_matrix
    case = next(x for x in report["cases"] if x["name"] == "bugfix")
    assert all(case["checks"][name] for name in ("goal_verified", "git_commit_created", "workflow_completed",
                  "restart_retained_approval", "report_has_goal_evidence", "report_tracks_git_head"))
    assert (directory / "bugfix" / "state" / "reports" / f"{case['run_id']}.json").is_file()


def test_feature_has_two_real_worker_commits(offline_matrix):
    """验证功能开发可以并行使用两个 Worker，并分别生成受限的 Git 文件变更。"""
    directory, report = offline_matrix
    case = next(x for x in report["cases"] if x["name"] == "feature")
    assert case["checks"]["goal_verified"] is True
    assert case["checks"]["lead_consumed_notifications"] is True
    assert case["checks"]["v5_events_persisted"] is True
    assert case["checks"]["real_v5_jobs_completed"] is True
    payload = json.loads((directory / "feature" / "state" / "reports" / f"{case['run_id']}.json").read_text(encoding="utf-8"))
    assert set(payload["changed_files"]) == {"slug.py", "helper.py"}
    assert "test_slug.py" not in payload["changed_files"]
    assert len(payload["worker_jobs"]) == 2


def test_negative_cases_refuse_false_delivery(offline_matrix):
    """验证审批拒绝、测试失败、路径穿越和证据过期都不能变成成功交付。"""
    _, report = offline_matrix
    guard = next(x for x in report["cases"] if x["name"] == "failure_guards")
    assert guard["checks"]["path_traversal_denied"] is True
    assert guard["checks"]["rejected_approval_blocks_work"] is True
    assert guard["checks"]["failed_test_blocks_delivery"] is True
    assert guard["checks"]["stale_evidence_blocks_delivery"] is True


def test_acceptance_report_saved_as_json_and_markdown(offline_matrix):
    """确认阶段结论保存为可审计 JSON 与 Markdown，而不依赖终端输出。"""
    directory, report = offline_matrix
    saved = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert saved == report
    assert "真实模型在线通过：False" in (directory / "result.md").read_text(encoding="utf-8")


def test_independent_cli_process_reads_persisted_workflow(offline_matrix):
    """使用独立 Python 子进程运行真实 CLI，确认能在进程退出后读取已完成会话。"""
    directory, report = offline_matrix
    case = next(x for x in report["cases"] if x["name"] == "bugfix")
    result = subprocess.run([sys.executable, "-m", "devpilot.cli", "status",
         "--repo", str(directory / "bugfix" / "repo"), "--state", str(directory / "bugfix" / "state"),
         "--run-id", case["run_id"]], text=True, capture_output=True, timeout=25, check=False)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["workflow"] == "completed"
    assert status["goal"] == "verified"


def test_online_flag_cannot_be_forged_by_offline_cases():
    """强制在线模式不能仅靠把离线案例的检查结果设置为真就宣称通过。"""
    fake = [{"name": x, "checks": {"okay": True}, "real_model_calls": 0, "error": None} for x in
            ("analysis", "bugfix", "feature", "failure_guards")]
    report = build_final_report("LIVE_DEEPSEEK", fake)
    assert report["online_passed"] is False
    assert report["offline_passed"] is False


def test_real_mcp_gate_requires_verified_tool_result():
    """要求真实 MCP 时不能仅凭已有工具发现或普通模型调用把验收判为成功。"""
    fake = [{"name": x, "checks": {"okay": True}, "real_model_calls": 2, "error": None}
            for x in ("analysis", "bugfix", "feature", "failure_guards")]
    assert build_final_report("LIVE_DEEPSEEK", fake, requires_mcp=True)["online_passed"] is False
    fake[1]["mcp_verified"] = True
    assert build_final_report("LIVE_DEEPSEEK", fake, requires_mcp=True)["online_passed"] is True


def test_bad_report_input_is_rejected():
    """缺少必需场景或复制重复场景的验收报告必须被拒绝。"""
    with pytest.raises(ValueError):
        build_final_report("LIVE_DEEPSEEK", [])
    with pytest.raises(ValueError):
        build_final_report("OFFLINE_SCRIPT_MODEL", [{"name": "analysis", "checks": {"ok": True}}])


def test_model_errors_are_redacted():
    """报告出现 API Key 等常见敏感字段时只保留已脱敏的异常摘要。"""
    message = safe_message(RuntimeError("DEEPSEEK_API_KEY=secret-12345 sk-1234567890abcdef"))
    assert "secret-12345" not in message and "sk-1234" not in message
    assert "[REDACTED]" in message


def test_fixture_never_overwrites_existing_repository(tmp_path):
    """验收脚本不得重置、清理或覆盖用户已经存在的 Git 仓库。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    with pytest.raises(FileExistsError):
        make_git_fixture(repo, scenarios()["feature"])
    assert (repo / "mathbox.py").exists()


def test_suite_rejects_nonempty_output_directory(tmp_path):
    """已有验收记录时禁止再次覆盖同一输出目录。"""
    root = tmp_path / "already"
    root.mkdir()
    (root / "result.json").write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_suite(root, live=False, authorize_fixture_approval=True)
    assert (root / "result.json").read_text(encoding="utf-8") == "old"


def test_mcp_config_cannot_be_mocked_offline(tmp_path):
    """要求真实 MCP 协议验收时，禁止使用离线模型冒充协议结果。"""
    with pytest.raises(ValueError):
        run_suite(tmp_path / "mcp", live=False, authorize_fixture_approval=True, mcp_config=tmp_path / "mcp.json")


def test_no_approval_leaves_changes_pending(tmp_path):
    """没有宿主明确批准时只生成持久化计划和审批单，不会修改仓库。"""
    case = scenarios()["bugfix"]
    result = run_coding_case(tmp_path, case, live=False, model_name="unused", authorize_fixture_approval=False)
    assert result["checks"]["nothing_changed_before_approval"] is True
    assert result["checks"]["preapproval_required"] is True
    assert result["run_id"] is not None
    assert (tmp_path / "repo" / "mathbox.py").read_text(encoding="utf-8") == case.broken


def test_doctor_is_readonly_and_never_reports_key(tmp_path, monkeypatch):
    """环境诊断只返回密钥是否配置，不输出密钥或向网络发送请求。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-dont-print")
    report = environment_report(repo, tmp_path / "state", require_live=True)
    assert "DEEPSEEK_API_KEY" not in json.dumps(report)
    assert "secret-dont-print" not in json.dumps(report)
    assert report["checks"]["repo_is_git_root"] is True
    assert report["checks"]["git_worktree_clean"] is True


def test_doctor_detects_dirty_repo(tmp_path):
    """真实 Git 仓库存在未提交修改时，诊断必须拒绝新运行前置条件。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    (repo / "mathbox.py").write_text("changed", encoding="utf-8")
    report = environment_report(repo, tmp_path / "state")
    assert report["ready"] is False
    assert report["checks"]["git_worktree_clean"] is False


def test_cli_doctor_works_without_api_key(tmp_path, monkeypatch, capsys):
    """CLI 的 doctor 子命令无需大模型密钥也可以检查真实仓库和状态目录。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        "devpilot.runtime.diagnostics._has_deepseek_key",
        lambda: False,
    )
    assert cli_main(["doctor", "--repo", str(repo), "--state", str(tmp_path / "state")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["offline_ready"] is True
    assert report["live_ready"] is False


def test_cli_doctor_requires_key_for_live(tmp_path, monkeypatch, capsys):
    """要求在线就绪时没有 DeepSeek Key 不得显示 ready，即便离线测试已经通过。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        "devpilot.runtime.diagnostics._has_deepseek_key",
        lambda: False,
    )
    assert cli_main(["doctor", "--repo", str(repo), "--state", str(tmp_path / "state"), "--require-live"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False


def test_doctor_recognizes_local_dotenv_without_exposing_values(tmp_path, monkeypatch):
    """本机只有 .env 配置 DeepSeek Key 时诊断仍能识别，而且不会泄露密钥内容。"""
    repo = tmp_path / "repo"
    make_git_fixture(repo, scenarios()["bugfix"])
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("DEEPSEEK_API_KEY=local-secret-do-not-print\n", encoding="utf-8")
    monkeypatch.chdir(config_dir)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    report = environment_report(repo, tmp_path / "state")
    assert report["checks"]["deepseek_api_key_configured"] is True
    assert "local-secret-do-not-print" not in json.dumps(report)
