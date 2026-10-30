"""测试 V8.2 的真实命令、文件、Git 证据和持久化边界。"""

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from devpilot.goal import (
    AcceptanceCriterion, EvidenceCollector, EvidenceStore, GoalRuntime,
    GoalSpec, GoalStore,
)


def make_goal(tmp_path: Path, *criteria: AcceptanceCriterion, git: bool = False):
    """创建临时工作区和独立的 GoalStore、EvidenceStore，准备测试采集条件。"""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    if git:
        if shutil.which("git") is None:
            pytest.skip("git is unavailable")
        git_run(workspace, "init", "-q")
        (workspace / "sample.py").write_text("VALUE=1\n", encoding="utf-8")
        git_run(workspace, "add", "sample.py")
        git_run(workspace, "-c", "user.name=DevPilot", "-c", "user.email=demo@example.invalid", "commit", "-qm", "init")
    goals = GoalStore(tmp_path / "goals")
    runtime = GoalRuntime(goals)
    run = runtime.start(GoalSpec("demo", "演示", "收集真实证据", criteria))
    runtime.begin_execution(run.id)
    runtime.begin_verification(run.id)
    evidence = EvidenceStore(tmp_path / "evidence")
    return workspace, goals, evidence, run.id


def git_run(workspace: Path, *args: str) -> str:
    """在独立测试仓库中运行受控 Git 子命令并返回标准输出。"""
    result = subprocess.run(["git", *args], cwd=workspace, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def criterion(id: str, kind: str, config: dict) -> AcceptanceCriterion:
    """为测试创建一条具备合法字段的验收规则。"""
    return AcceptanceCriterion(id=id, description=id, kind=kind, config=config)


def make_collector(workspace: Path, goals: GoalStore, evidence: EvidenceStore, **kwargs):
    """用临时工作区创建受限制的 EvidenceCollector。"""
    return EvidenceCollector(goals, evidence, workspace, **kwargs)


def test_file_exists_records_file_fact_without_passing_criterion(tmp_path):
    """验证文件存在证据不会偷偷修改 Goal 的验收状态。"""
    c = criterion("exists", "file_exists", {"path": "README.md"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    (root / "README.md").write_text("你好", encoding="utf-8")
    record = make_collector(root, goals, evidence).collect(run_id, "exists")
    assert record.status == "collected" and record.observation["exists"] is True
    assert len(record.observation["sha256"]) == 64
    assert len(record.goal_spec_sha256) == 64
    assert goals.get(run_id).criteria["exists"].status == "unverified"
    assert goals.get(run_id).status == "verifying"


def test_missing_file_is_a_fact_not_transport_error(tmp_path):
    """验证找不到文件时仍记录明确的不存在事实。"""
    c = criterion("exists", "file_exists", {"path": "missing.py"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence).collect(run_id, "exists")
    assert record.status == "collected" and record.observation["exists"] is False


def test_file_contains_true_and_false(tmp_path):
    """验证文件包含条件能够记录命中与未命中的实际观察值。"""
    c = criterion("contains", "file_contains", {"path": "app.py", "contains": "return 401"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    (root / "app.py").write_text("return 500\n", encoding="utf-8")
    collector = make_collector(root, goals, evidence)
    first = collector.collect(run_id, "contains")
    (root / "app.py").write_text("return 401\n", encoding="utf-8")
    second = collector.collect(run_id, "contains")
    assert first.observation["contains_target"] is False
    assert second.observation["contains_target"] is True
    assert first.observation["sha256"] != second.observation["sha256"]
    assert len(evidence.list_for_goal(run_id)) == 2


def test_file_too_large_is_unknown(tmp_path):
    """验证超过读取上限的文件会产生未知状态而非错误通过。"""
    c = criterion("exists", "file_exists", {"path": "big.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    (root / "big.txt").write_bytes(b"a" * 24)
    record = make_collector(root, goals, evidence, max_file_bytes=8).collect(run_id, "exists")
    assert record.status == "unknown" and record.observation["size_bytes"] == 24


def test_non_utf8_file_contains_is_unknown(tmp_path):
    """验证无法按 UTF-8 解码的内容不会被错误判断为不包含目标文本。"""
    c = criterion("contains", "file_contains", {"path": "bad.txt", "contains": "abc"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    (root / "bad.txt").write_bytes(b"\xff\xfe")
    record = make_collector(root, goals, evidence).collect(run_id, "contains")
    assert record.status == "unknown"


def test_symlink_escape_is_denied(tmp_path):
    """验证已通过文本路径校验的符号链接仍不能跳出工作区。"""
    c = criterion("exists", "file_exists", {"path": "link.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink permissions unavailable on this platform")
    with pytest.raises(PermissionError):
        make_collector(root, goals, evidence).collect(run_id, "exists")
    assert evidence.list_for_goal(run_id) == []


def test_command_is_deny_by_default(tmp_path):
    """验证未在宿主白名单里的命令绝不能被执行。"""
    marker = tmp_path / "executed.txt"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('executed')"]
    c = criterion("cmd", "command_exit_code", {"argv": argv})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    with pytest.raises(PermissionError):
        make_collector(root, goals, evidence).collect(run_id, "cmd")
    assert not marker.exists()
    assert evidence.list_for_goal(run_id) == []


def test_command_runs_exact_allowed_argv_and_captures_exit(tmp_path):
    """验证只执行完整匹配白名单的 argv，并保存真实返回码与日志。"""
    argv = [sys.executable, "-c", "print('actual test passed')"]
    c = criterion("cmd", "command_exit_code", {"argv": argv, "expected_exit_code": 0})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence, allowed_commands=(tuple(argv),)).collect(run_id, "cmd")
    assert record.observation["returncode"] == 0
    assert "actual test passed" in record.observation["stdout_tail"]
    assert record.observation["stdout_sha256"] == hashlib.sha256(b"actual test passed\n").hexdigest()
    assert record.status == "collected"
    assert goals.get(run_id).criteria["cmd"].status == "unverified"


def test_nonzero_exit_is_collected_not_automatically_failed(tmp_path):
    """验证退出码不为零只是已采集事实，正式失败结论留给 V8.3。"""
    argv = [sys.executable, "-c", "import sys;sys.exit(3)"]
    c = criterion("cmd", "command_exit_code", {"argv": argv})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence, allowed_commands=(tuple(argv),)).collect(run_id, "cmd")
    assert record.status == "collected" and record.observation["returncode"] == 3


def test_command_timeout_returns_unknown(tmp_path):
    """验证命令超时记录为结果未知，绝不会冒充明确失败。"""
    argv = [sys.executable, "-c", "import time;time.sleep(1)"]
    c = criterion("cmd", "command_exit_code", {"argv": argv})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence, allowed_commands=(tuple(argv),), command_timeout=0.03).collect(run_id, "cmd")
    assert record.status == "unknown"
    assert record.observation["returncode"] is None
    assert "timed out" in record.observation["reason"]


def test_command_output_is_capped_and_hashed(tmp_path):
    """验证大量测试日志只保存有限尾部内容，同时保留完整输出哈希。"""
    argv = [sys.executable, "-c", "print('A'*2000);print('END')"]
    c = criterion("cmd", "command_exit_code", {"argv": argv})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence, allowed_commands=(tuple(argv),), output_limit_bytes=128).collect(run_id, "cmd")
    assert record.observation["stdout_truncated"] is True
    assert record.observation["stdout_bytes"] > 128
    assert "END" in record.observation["stdout_tail"]
    assert len(record.observation["stdout_tail"].encode()) <= 130


def test_command_not_found_returns_unknown(tmp_path):
    """验证可信命令不存在时保存启动失败事实而不是让 Goal 意外通过。"""
    argv = ["__devpilot_unavailable_command_99831__"]
    c = criterion("cmd", "command_exit_code", {"argv": argv})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence, allowed_commands=(tuple(argv),)).collect(run_id, "cmd")
    assert record.status == "unknown" and "could not start" in record.observation["reason"]


def test_git_head_collects_real_sha(tmp_path):
    """验证 Git HEAD 来自真实 Git 仓库，并写入证据元信息。"""
    c = criterion("commit", "git_head", {"expected_sha": "abcdef1"})
    root, goals, evidence, run_id = make_goal(tmp_path, c, git=True)
    record = make_collector(root, goals, evidence).collect(run_id, "commit")
    expected = git_run(root, "rev-parse", "HEAD")
    assert record.observation["head"] == expected
    assert record.git_head_before == expected == record.git_head_after
    assert record.git_dirty_before is False


def test_non_git_workspace_marks_head_unknown(tmp_path):
    """验证普通目录没有 Git HEAD 时仍记录证据不足。"""
    c = criterion("commit", "git_head", {"expected_sha": "abcdef1"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence).collect(run_id, "commit")
    assert record.status == "unknown" and record.git_head_before is None


def test_manual_criterion_requires_trusted_human(tmp_path):
    """验证 manual 条件不能被本地自动采集器伪造成已核验。"""
    manual = criterion("manual", "manual", {"instructions": "review"})
    root, goals, evidence, run_id = make_goal(tmp_path, manual)
    with pytest.raises(PermissionError):
        make_collector(root, goals, evidence).collect(run_id, "manual")
    assert evidence.list_for_goal(run_id) == []


def test_collect_all_ignores_manual_and_keeps_goal_unverified(tmp_path):
    """验证批量采集只处理自动条件，保留人工条件和 Goal 原状态。"""
    c1 = criterion("a", "file_exists", {"path": "a.txt"})
    c2 = criterion("m", "manual", {"instructions": "review"})
    c3 = criterion("b", "file_exists", {"path": "b.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c1, c2, c3)
    output = make_collector(root, goals, evidence).collect_all(run_id)
    assert [record.criterion_id for record in output] == ["a", "b"]
    assert all(record.status == "collected" for record in output)
    assert all(r.status == "unverified" for r in goals.get(run_id).criteria.values())


def test_collect_disallowed_before_verification(tmp_path):
    """验证 Goal 未进入 verifying 状态时禁止提前采集正式验收证据。"""
    root = tmp_path / "repo"
    root.mkdir()
    goals = GoalStore(tmp_path / "goals")
    run = GoalRuntime(goals).start(GoalSpec("demo", "目标", "说明", (criterion("a", "file_exists", {"path": "a.txt"}),)))
    evidence = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError):
        make_collector(root, goals, evidence).collect(run.id, "a")


def test_unknown_criterion_rejected(tmp_path):
    """验证无法向不存在的验收项添加证据。"""
    c = criterion("a", "file_exists", {"path": "a.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    with pytest.raises(KeyError):
        make_collector(root, goals, evidence).collect(run_id, "unknown")


def test_evidence_store_survives_restart_and_is_append_only(tmp_path):
    """验证同一 Goal 多次采集不会覆盖旧证据，重启后还能按 ID 查询。"""
    c = criterion("a", "file_exists", {"path": "a.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    collector = make_collector(root, goals, evidence)
    first = collector.collect(run_id, "a")
    (root / "a.txt").write_text("new", encoding="utf-8")
    second = collector.collect(run_id, "a")
    restarted = EvidenceStore(tmp_path / "evidence")
    assert restarted.get(run_id, first.id) == first
    assert restarted.get(run_id, second.id) == second
    assert first.observation["exists"] is False
    assert second.observation["exists"] is True
    with pytest.raises(FileExistsError):
        restarted.append(first)


def test_evidence_path_traversal_rejected(tmp_path):
    """验证 Goal ID 和 Evidence ID 不能绕过证据目录访问其他文件。"""
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError):
        store.get("../parent", "0" * 32)
    with pytest.raises(ValueError):
        store.get("a" * 32, "../file")
    with pytest.raises(ValueError):
        store.list_for_goal("../bad")


def test_evidence_store_filters_criterion(tmp_path):
    """验证 UI 和未来 Verifier 能按验收条件查找相应证据。"""
    c1 = criterion("a", "file_exists", {"path": "a.txt"})
    c2 = criterion("b", "file_exists", {"path": "b.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c1, c2)
    make_collector(root, goals, evidence).collect_all(run_id)
    assert len(evidence.list_for_goal(run_id)) == 2
    assert [r.criterion_id for r in evidence.list_for_goal(run_id, "a")] == ["a"]
    assert len(evidence.list_for_goal(run_id, "unknown")) == 0


def test_collector_rejects_invalid_limits_and_workspace(tmp_path):
    """验证无法给采集器配置不存在的工作区或无效资源限制。"""
    goals = GoalStore(tmp_path / "goals")
    evidence = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(FileNotFoundError):
        EvidenceCollector(goals, evidence, tmp_path / "missing")
    with pytest.raises(ValueError):
        EvidenceCollector(goals, evidence, tmp_path, output_limit_bytes=4)
    with pytest.raises(ValueError):
        EvidenceCollector(goals, evidence, tmp_path, allowed_commands=(("",),))


def test_saved_evidence_is_valid_json(tmp_path):
    """验证保存的证据文件可以用普通 JSON 工具读取。"""
    c = criterion("a", "file_exists", {"path": "hello.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    record = make_collector(root, goals, evidence).collect(run_id, "a")
    file = tmp_path / "evidence" / run_id / f"{record.id}.json"
    raw = json.loads(file.read_text(encoding="utf-8"))
    assert raw["goal_run_id"] == run_id and raw["criterion_id"] == "a"


def test_git_head_change_during_capture_is_unknown(tmp_path, monkeypatch):
    """验证采集期间发生代码版本变化时必须保守标记结果未知。"""
    c = criterion("a", "file_exists", {"path": "a.txt"})
    root, goals, evidence, run_id = make_goal(tmp_path, c)
    collector = make_collector(root, goals, evidence)
    snapshots = iter([("a" * 40, False), ("b" * 40, False)])
    monkeypatch.setattr(collector, "_git_state", lambda: next(snapshots))
    record = collector.collect(run_id, "a")
    assert record.status == "unknown"
    assert "changed" in record.observation["reason"]


def test_git_dirty_state_saved_for_file_observations(tmp_path):
    """验证修改未提交的受控工作区文件会被标记为 Git 脏状态。"""
    c = criterion("a", "file_contains", {"path": "sample.py", "contains": "VALUE=2"})
    root, goals, evidence, run_id = make_goal(tmp_path, c, git=True)
    (root / "sample.py").write_text("VALUE=2\n", encoding="utf-8")
    record = make_collector(root, goals, evidence).collect(run_id, "a")
    assert record.status == "collected"
    assert record.observation["contains_target"] is True
    assert record.git_dirty_before is True and record.git_dirty_after is True
