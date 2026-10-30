"""V8.3：用真实 Git 仓库与 V8.2 证据测试规则判定、来源校验和状态持久化。"""

import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from devpilot.goal import (
    AcceptanceCriterion, EvidenceCollector, EvidenceStore, GoalRuntime,
    GoalSpec, GoalStore, GoalVerifier,
)


def git(repo: Path, *args: str) -> str:
    """在测试 Git 仓库运行受控命令并获取标准输出。"""
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    """准备有真实 Git Commit 的隔离工作区、持久化 Goal、证据库和验收器。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auth.py").write_text('def login(password):\n    return 401 if password == "bad" else 200\n', encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "auth.py")
    git(repo, "-c", "user.name=Tester", "-c", "user.email=demo@example.invalid", "commit", "-qm", "initial")
    head = git(repo, "rev-parse", "HEAD")
    stores = GoalStore(tmp_path / "state" / "goals")
    evidence = EvidenceStore(tmp_path / "state" / "evidence")
    runtime = GoalRuntime(stores)
    verifier = GoalVerifier(stores, evidence, repo)
    return repo, head, stores, evidence, runtime, verifier


def start(env, criteria=None):
    """创建一份已经进入 verifying 状态的目标供测试使用。"""
    repo, head, stores, evidence, runtime, verifier = env
    if criteria is None:
        criteria = (AcceptanceCriterion("file", "auth.py 存在", "file_exists", {"path": "auth.py"}),)
    spec = GoalSpec("login_bug", "登录缺陷", "验收真实代码", tuple(criteria))
    run = runtime.start(spec)
    runtime.begin_execution(run.id)
    runtime.begin_verification(run.id)
    return run.id


def collect(env, run_id, allowed_commands=()):
    """调用真正的 V8.2 EvidenceCollector 生成持久化证据。"""
    repo, head, goals, evidence, runtime, verifier = env
    collector = EvidenceCollector(goals, evidence, repo, allowed_commands=tuple(allowed_commands))
    return collector.collect_all(run_id)


def test_real_file_evidence_verifies_and_persists(env):
    """真实文件证据通过后会把必选条件和 Goal 一起持久化为成功。"""
    run_id = start(env)
    records = collect(env, run_id)
    report = env[5].verify(run_id)
    assert report.status == "verified"
    assert report.criteria[0].status == "pass"
    assert report.criteria[0].evidence_id == records[0].id
    from_disk = GoalStore(env[2].root).get(run_id)
    assert from_disk.status == "verified"
    assert from_disk.criteria["file"].evidence_ids == [records[0].id]
    assert from_disk.events[-1].event_type == "goal_verified"


def test_missing_evidence_is_unknown(env):
    """没有采集证据时不能把 Goal 判为成功。"""
    run_id = start(env)
    report = env[5].verify(run_id)
    assert report.status == "unknown"
    assert report.criteria[0].status == "unknown"
    assert env[2].get(run_id).criteria["file"].evidence_ids == []


def test_missing_file_is_fail_not_unknown(env):
    """不存在的文件有明确事实时应当判 FAIL 而不是 UNKNOWN。"""
    run_id = start(env, (AcceptanceCriterion("missing", "应有文件", "file_exists", {"path": "missing.py"}),))
    collect(env, run_id)
    report = env[5].verify(run_id)
    assert report.status == "verifying"
    assert report.criteria[0].status == "fail"


def test_file_contains_pass_and_fail(env):
    """文件内容匹配应基于真实采集的 contains_target 进行判断。"""
    criteria = (
        AcceptanceCriterion("yes", "包含状态码", "file_contains", {"path": "auth.py", "contains": "401"}),
        AcceptanceCriterion("no", "包含不存在文本", "file_contains", {"path": "auth.py", "contains": "UNSEEN"}),
    )
    run_id = start(env, criteria)
    collect(env, run_id)
    report = env[5].verify(run_id)
    assert [(c.criterion_id, c.status) for c in report.criteria] == [("yes", "pass"), ("no", "fail")]
    assert report.status == "verifying"


def test_actual_command_exit_zero(env):
    """经过白名单批准的真实命令返回零时才验收通过。"""
    argv = [sys.executable, "-B", "-c", "print('ok')"]
    run_id = start(env, (AcceptanceCriterion("test", "命令成功", "command_exit_code", {"argv": argv, "expected_exit_code": 0}),))
    collect(env, run_id, allowed_commands=(tuple(argv),))
    report = env[5].verify(run_id)
    assert report.status == "verified"
    assert report.criteria[0].status == "pass"


def test_real_nonzero_exit_is_fail(env):
    """有明确非零退出码时必须按目标期望判定失败。"""
    argv = [sys.executable, "-B", "-c", "import sys;sys.exit(3)"]
    run_id = start(env, (AcceptanceCriterion("test", "命令应成功", "command_exit_code", {"argv": argv, "expected_exit_code": 0}),))
    collect(env, run_id, allowed_commands=(tuple(argv),))
    report = env[5].verify(run_id)
    assert report.criteria[0].status == "fail"
    assert report.status == "verifying"


def test_real_git_prefix_matches(env):
    """git_head 规则允许验证当前 HEAD 的合法缩写前缀。"""
    run_id = start(env, (AcceptanceCriterion("commit", "核对版本", "git_head", {"expected_sha": env[1][:12]}),))
    collect(env, run_id)
    assert env[5].verify(run_id).status == "verified"


def test_git_expected_sha_mismatch_is_fail(env):
    """Git 当前版本与预期 SHA 不一致应判定明确失败。"""
    prefix = "0000000" if not env[1].startswith("0000000") else "1111111"
    run_id = start(env, (AcceptanceCriterion("commit", "核对版本", "git_head", {"expected_sha": prefix}),))
    collect(env, run_id)
    assert env[5].verify(run_id).criteria[0].status == "fail"


def test_required_manual_stays_unknown(env):
    """必选人工条件即使自动采集完成也必须等待可信人工证据。"""
    criteria = (
        AcceptanceCriterion("file", "目标文件", "file_exists", {"path": "auth.py"}),
        AcceptanceCriterion("review", "人工审核", "manual", {"instructions": "审阅代码"}),
    )
    run_id = start(env, criteria)
    collect(env, run_id)
    report = env[5].verify(run_id)
    assert report.status == "unknown"
    assert report.criteria[0].status == "pass"
    assert report.criteria[1].status == "unknown"


def test_optional_manual_does_not_block(env):
    """非必选人工条件可以暂时未知而不阻止必选条件全部通过的目标。"""
    criteria = (
        AcceptanceCriterion("file", "目标文件", "file_exists", {"path": "auth.py"}),
        AcceptanceCriterion("review", "可选人工审核", "manual", {"instructions": "审阅代码"}, required=False),
    )
    run_id = start(env, criteria)
    collect(env, run_id)
    assert env[5].verify(run_id).status == "verified"


def test_git_head_change_after_evidence_rejects_old_success(env):
    """采集成功后重新提交代码会使旧 PASS 证据变成未知。"""
    run_id = start(env)
    collect(env, run_id)
    (env[0] / "auth.py").write_text("print('new')\n", encoding="utf-8")
    git(env[0], "add", "auth.py")
    git(env[0], "-c", "user.name=Tester", "-c", "user.email=demo@example.invalid", "commit", "-qm", "changed")
    report = env[5].verify(run_id)
    assert report.status == "unknown"
    assert "Git HEAD" in report.criteria[0].reason


def test_dirty_workspace_rejects_success(env):
    """工作区被修改但没有提交时即使之前证据是成功也不能通过。"""
    run_id = start(env)
    collect(env, run_id)
    (env[0] / "auth.py").write_text("print('dirty')", encoding="utf-8")
    assert env[5].verify(run_id).status == "unknown"


def test_dirty_during_collection_rejects_even_after_cleanup(env):
    """证据采集时工作区脏过，即使后来恢复干净也不能信任该证据。"""
    run_id = start(env)
    (env[0] / "auth.py").write_text("dirty", encoding="utf-8")
    collect(env, run_id)
    git(env[0], "checkout", "--", "auth.py")
    assert env[5].verify(run_id).status == "unknown"


def test_provenance_spec_hash_mismatch(env):
    """目标定义指纹不一致的证据不会被判为 PASS。"""
    run_id = start(env)
    original = collect(env, run_id)[0]
    newer = replace(original, id=uuid4().hex, captured_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), goal_spec_sha256="0" * 64)
    env[3].append(newer)
    assert env[5].verify(run_id).status == "unknown"


def test_provenance_workspace_mismatch(env):
    """从另一个工作区复制来的证据不能为本工作区验收。"""
    run_id = start(env)
    original = collect(env, run_id)[0]
    newer = replace(original, id=uuid4().hex, captured_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), workspace="/other/project")
    env[3].append(newer)
    assert env[5].verify(run_id).status == "unknown"


def test_latest_evidence_does_not_fall_back_to_older_success(env):
    """最新一份证据明确失败时不能退回使用更旧的成功证据。"""
    run_id = start(env)
    original = collect(env, run_id)[0]
    newer = replace(original, id=uuid4().hex, captured_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), observation={"path": "auth.py", "exists": False})
    env[3].append(newer)
    report = env[5].verify(run_id)
    assert report.status == "verifying"
    assert report.criteria[0].status == "fail"
    assert env[2].get(run_id).criteria["file"].evidence_ids == [newer.id]


def test_latest_unknown_does_not_fall_back_to_older_success(env):
    """最新证据状态未知时不能沿用较早的 PASS。"""
    run_id = start(env)
    original = collect(env, run_id)[0]
    newer = replace(original, id=uuid4().hex, captured_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), status="unknown")
    env[3].append(newer)
    assert env[5].verify(run_id).status == "unknown"


def test_unknown_resume_requires_new_evidence(env):
    """从 unknown 重新开始核验后必须采集新证据，不能沿用上一轮。"""
    run_id = start(env)
    collect(env, run_id)
    original = env[5]._git_state
    env[5]._git_state = lambda: (None, None)
    assert env[5].verify(run_id).status == "unknown"
    env[5]._git_state = original
    env[4].resume_verification(run_id)
    assert env[5].verify(run_id).status == "unknown"
    env[4].resume_verification(run_id)
    collect(env, run_id)
    assert env[5].verify(run_id).status == "verified"


def test_verify_requires_verifying_state(env):
    """不能跳过执行与采集阶段直接把 pending 目标认证为完成。"""
    spec = GoalSpec("goal", "目标", "说明", (AcceptanceCriterion("file", "存在文件", "file_exists", {"path": "auth.py"}),))
    run = env[4].start(spec)
    with pytest.raises(ValueError, match="verifying"):
        env[5].verify(run.id)


def test_git_change_during_verification_forces_unknown(env, monkeypatch):
    """验收前后两次读取 Git 状态若不一致则全部条件必须未知。"""
    run_id = start(env)
    collect(env, run_id)
    calls = iter([(env[1], False), ("f" * 40, False)])
    monkeypatch.setattr(env[5], "_git_state", lambda: next(calls))
    report = env[5].verify(run_id)
    assert report.status == "unknown"
    assert all(item.status == "unknown" for item in report.criteria)


def test_no_git_repository_is_unknown(env, monkeypatch):
    """无法确认当前 Git 版本时禁止产生 VERIFIED。"""
    run_id = start(env)
    collect(env, run_id)
    monkeypatch.setattr(env[5], "_git_state", lambda: (None, None))
    assert env[5].verify(run_id).status == "unknown"


def test_pre_verification_evidence_is_unknown(env):
    """即使代码没变，本轮验证开始前的旧证据也不能直接复用。"""
    run_id = start(env)
    old = collect(env, run_id)[0]
    ancient = replace(old, id=uuid4().hex, captured_at="2020-01-01T00:00:00+00:00")
    env[3].append(ancient)
    # 人为重建为最新证据：更早时间会被排序到前面，此处通过清空旧文件验证时间窗口。
    (env[3].root / run_id / f"{old.id}.json").unlink()
    assert env[5].verify(run_id).status == "unknown"


def test_bad_argument_observation_is_unknown(env):
    """采集命令和验收配置不同的证据不得当作测试通过。"""
    argv = [sys.executable, "-B", "-c", "print(1)"]
    run_id = start(env, (AcceptanceCriterion("test", "命令执行", "command_exit_code", {"argv": argv}),))
    original = collect(env, run_id, allowed_commands=(tuple(argv),))[0]
    newer = replace(original, id=uuid4().hex, captured_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), observation={**original.observation, "argv": ["echo", "wrong"]})
    env[3].append(newer)
    assert env[5].verify(run_id).criteria[0].status == "unknown"


def test_goal_store_version_increases_after_verification(env):
    """每次核验都产生持久化新版本，方便 UI 观察变化。"""
    run_id = start(env)
    collect(env, run_id)
    before = env[2].get(run_id).version
    env[5].verify(run_id)
    after = env[2].get(run_id).version
    assert after == before + 1


def test_optional_fail_does_not_block_required_pass(env):
    """只有必选条件决定整体通过，可选条件的失败仍会被完整记录。"""
    criteria = (
        AcceptanceCriterion("required", "存在文件", "file_exists", {"path": "auth.py"}),
        AcceptanceCriterion("optional", "可选文件", "file_exists", {"path": "missing.py"}, required=False),
    )
    run_id = start(env, criteria)
    collect(env, run_id)
    report = env[5].verify(run_id)
    assert report.status == "verified"
    assert report.criteria[1].status == "fail"


def test_unknown_result_and_fail_precedence(env):
    """必选条件既有明确失败又有未知时不能认证成功，并保留全部原因。"""
    criteria = (
        AcceptanceCriterion("missing", "缺少文件", "file_exists", {"path": "missing.py"}),
        AcceptanceCriterion("manual", "未审核", "manual", {"instructions": "确认需求"}),
    )
    run_id = start(env, criteria)
    collect(env, run_id)
    report = env[5].verify(run_id)
    assert report.status == "verifying"
    assert [c.status for c in report.criteria] == ["fail", "unknown"]
