"""V9.2：验收工具的离线确定性测试，所有用例都不会连接真实 DeepSeek。"""

from pathlib import Path
import json

import pytest

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.tasks.store import TaskStore
from examples.v9_2_online_acceptance import (
    ScriptedSelfTestModel,
    ToolProbe,
    create_fixture,
    main,
    run_acceptance,
    safe_error,
    write_report,
)
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.registry import ToolRegistry


def test_offline_probe_exercises_real_v0_to_v3(tmp_path: Path):
    """验证脚本模型确实通过真实 Agent、Dispatcher、Memory 和 TaskStore 执行。"""
    repo = create_fixture(tmp_path / "repo")
    before = (repo / "auth.py").read_bytes()
    report = run_acceptance(repo, tmp_path / "state", lambda _id: ScriptedSelfTestModel(),
                            online=False, model_name="SCRIPTED", include_analysis=True)
    assert report["passed"] is False
    assert report["self_test_passed"] is True
    assert report["live_evidence"] is False
    assert report["model_complete_calls"] >= 7
    assert all(report["checks"].values())
    assert [event["name"] for event in report["v1_dispatcher_events"]] == ["glob", "read_file", "memory_search", "task_create"]
    assert len(TaskStore(tmp_path / "state" / "tasks.json").all()) == 1
    assert (repo / "auth.py").read_bytes() == before
    assert all("reply_preview" in phase for phase in report["phase_results"])


def test_no_tool_calls_is_a_failed_acceptance(tmp_path: Path):
    """模型即使声称修复完成，只要没有实际工具调用，验收就应失败。"""
    class TalkingOnlyModel:
        def complete(self, messages, tools=None, system=None):
            """仅返回自然语言，模拟没有真实动作的模型。"""
            return ModelResponse("所有工具已经调用了")
    repo = create_fixture(tmp_path / "repo")
    report = run_acceptance(repo, tmp_path / "state", lambda _id: TalkingOnlyModel(),
                            online=False, model_name="TALKING")
    assert report["passed"] is False
    assert report["self_test_passed"] is False
    assert report["checks"]["v1_read_file_executed"] is False
    assert report["checks"]["v3_task_persisted"] is False


def test_denied_or_unknown_tool_not_counted(tmp_path: Path):
    """工具不存在或权限拒绝时不能伪装成真实 Handler 返回成功。"""
    probe = ToolProbe(ToolDispatcher(ToolRegistry(), HookManager()), "marker", "auth.py")
    probe.install()
    assert "unknown tool" in probe.execute(ToolCall("x", "no_such_tool", {}))
    assert probe.events[0]["status"] == "unknown_tool"
    assert probe.events[0]["returned_error_text"] is True


def test_error_message_redacts_api_key(monkeypatch):
    """错误信息中出现用户的密钥时，应在报告中脱敏。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-verysecretkey123456")
    assert "sk-verysecretkey123456" not in safe_error(RuntimeError("secret sk-verysecretkey123456"))


def test_user_repository_requires_explicit_opt_in(tmp_path: Path):
    """用户自有代码仓库没有显式许可时不能被送到外部模型。"""
    repo = create_fixture(tmp_path / "repo")
    with pytest.raises(SystemExit):
        main(["--offline-self-test", "--repo", str(repo), "--output", str(tmp_path / "reports")])
    assert not (tmp_path / "reports").exists()


def test_live_rejects_missing_key_before_network(monkeypatch, tmp_path: Path):
    """没有 API Key 时禁止假装在线验收并且不发网络请求。"""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(["--live", "--output", str(tmp_path / "reports")])
    assert not (tmp_path / "reports").exists()


def test_report_writes_only_metadata_when_user_repo(tmp_path: Path):
    """验收报告默认只保存工具状态，不把用户仓库的源码或 Prompt 落盘。"""
    output = tmp_path / "result"
    report = {
        "run_id": "test", "mode": "offline-self-test", "passed": False,
        "model_complete_calls": 2, "checks": {"v1_read_file_executed": True},
        "phase_results": [{"phase": "inspect", "completed": True, "tool_names": ["read_file"]}],
    }
    write_report(output, report)
    text = (output / "result.md").read_text(encoding="utf-8")
    assert "v1_read_file_executed" in text
    assert json.loads((output / "result.json").read_text(encoding="utf-8"))["mode"] == "offline-self-test"
    assert "## inspect 模型回复" not in text


def test_fixture_repository_is_real_git(tmp_path: Path):
    """确保用于在线验收的演示仓库有真实 Git 元数据和可读取的 Bug 源码。"""
    repo = create_fixture(tmp_path / "demo")
    assert (repo / ".git").exists()
    assert "return 500" in (repo / "auth.py").read_text(encoding="utf-8")
    assert "401" in (repo / "README.md").read_text(encoding="utf-8")
