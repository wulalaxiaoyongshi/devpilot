"""V8.1 Goal 基础模块的确定性测试，不依赖模型、网络或外部工具。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devpilot.goal import (
    AcceptanceCriterion, GoalConflictError, GoalRuntime, GoalSpec, GoalStore, validate_spec,
)


def criterion(kind="command_exit_code", *, criterion_id="login_401", config=None, required=True):
    """生成一条可反复复用的验收条件，方便组合不同测试场景。"""
    defaults = {
        "command_exit_code": {"argv": ["python", "-m", "pytest", "tests/test_auth.py", "-q"], "expected_exit_code": 0},
        "file_exists": {"path": "tests/test_auth.py"},
        "file_contains": {"path": "src/auth.py", "contains": "401"},
        "git_head": {"expected_sha": "abc1234"},
        "manual": {"instructions": "审核代码差异"},
    }
    return AcceptanceCriterion(criterion_id, "密码错误返回 401", kind, defaults[kind] if config is None else config, required)


def spec(*items):
    """为测试生成一份登录错误修复 Goal 定义。"""
    return GoalSpec("fix-login-401", "修复登录接口", "确保错误密码返回 401 且保留回归测试", tuple(items) if items else (criterion(),))


def make_runtime(tmp_path):
    """使用临时目录创建真实 JSON Store 和可预测时间的 GoalRuntime。"""
    clock = lambda: datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)
    return GoalRuntime(GoalStore(tmp_path / "goals"), clock=clock)


def test_valid_spec_for_all_kinds():
    """验证所有预设验收类型都能通过格式检查。"""
    validate_spec(spec(*(criterion(kind, criterion_id=f"check_{i}") for i, kind in enumerate([
        "command_exit_code", "file_exists", "file_contains", "git_head", "manual"
    ]))))


def test_duplicate_criterion_rejected():
    """验证重复的验收 ID 不允许写入同一目标。"""
    with pytest.raises(ValueError, match="Duplicate"):
        validate_spec(spec(criterion(), criterion()))


def test_invalid_goal_id_rejected():
    """验证非法 Goal ID 在启动前被拒绝。"""
    with pytest.raises(ValueError):
        validate_spec(replace(spec(), id="../../bad"))


def test_empty_criteria_rejected():
    """验证没有任何验收条件的目标无法启动。"""
    with pytest.raises(ValueError, match="at least one"):
        validate_spec(spec().__class__("goal", "title", "desc", ()))


def test_no_required_criteria_rejected():
    """验证 Goal 至少要有一个必须满足的验收条件。"""
    with pytest.raises(ValueError, match="required"):
        validate_spec(spec(criterion(required=False)))


def test_unknown_kind_rejected():
    """验证未支持的验收类型会明确失败。"""
    with pytest.raises(ValueError, match="Unsupported"):
        validate_spec(spec(replace(criterion(), kind="untrusted_python")))


@pytest.mark.parametrize("config", [
    {"argv": []},
    {"argv": "pytest"},
    {"argv": ["python", 3]},
    {"argv": ["python"], "expected_exit_code": True},
    {"argv": ["python"], "shell": True},
])
def test_command_configuration_rejected(config):
    """验证命令验收只能使用明确 argv 和整数退出码等规定字段。"""
    with pytest.raises((ValueError, TypeError)):
        validate_spec(spec(criterion(config=config)))


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret.txt", "src/../../secret", "C:\\Windows\\secret", "C:secret", "", "src//auth.py"])
def test_file_path_rejected(path):
    """验证绝对路径、磁盘驱动器和父目录穿越路径会被拒绝。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion("file_exists", config={"path": path})))


def test_file_contains_needs_nonempty_text():
    """验证文件内容条件必须提供具体的匹配字符串。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion("file_contains", config={"path": "a.py", "contains": ""})))


def test_git_sha_must_be_hex():
    """验证 Git 版本条件必须是受支持的十六进制提交编号。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion("git_head", config={"expected_sha": "not-a-sha"})))


def test_manual_needs_instruction():
    """验证人工检查条件必须描述检查什么。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion("manual", config={"instructions": "  "})))


def test_non_json_configuration_rejected():
    """验证集合等无法保存为 JSON 的参数会被拒绝。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion(config={"argv": ["python"], "bogus": {1, 2}})))


def test_nan_configuration_rejected():
    """验证 NaN 不允许进入可持久化的 Goal 规则。"""
    with pytest.raises(ValueError):
        validate_spec(spec(criterion(config={"argv": ["python"], "expected_exit_code": float("nan")})))


def test_goal_create_has_unverified_conditions(tmp_path):
    """验证新 Goal 创建后仍处于 pending，且全部条件都尚未验证。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec(criterion(), criterion("file_exists", criterion_id="test_exists")))
    assert run.status == "pending"
    assert run.criteria["login_401"].status == "unverified"
    assert run.criteria["test_exists"].status == "unverified"
    assert len(run.events) == 1
    assert (tmp_path / "goals" / f"{run.id}.json").is_file()


def test_goal_survives_restart_and_preserves_chinese(tmp_path):
    """验证重新创建 GoalStore 后仍能读取中文目标和原始输入。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec(), {"issue": "密码错误必须返回401"})
    reopened = make_runtime(tmp_path)
    restored = reopened.get(run.id)
    assert restored.spec.title == "修复登录接口"
    assert restored.inputs["issue"] == "密码错误必须返回401"
    assert restored.version == 0


def test_input_must_be_json_object(tmp_path):
    """验证目标输入必须是可 JSON 化的字典。"""
    runtime = make_runtime(tmp_path)
    with pytest.raises(TypeError):
        runtime.start(spec(), ["not", "dict"])
    with pytest.raises(ValueError):
        runtime.start(spec(), {"nan": float("nan")})


def test_create_makes_independent_input_snapshot(tmp_path):
    """验证宿主随后修改原始输入不会影响已持久化的目标。"""
    runtime = make_runtime(tmp_path)
    inputs = {"metadata": {"issue": "A"}}
    run = runtime.start(spec(), inputs)
    inputs["metadata"]["issue"] = "B"
    assert runtime.get(run.id).inputs["metadata"]["issue"] == "A"


def test_get_returns_independent_snapshot(tmp_path):
    """验证修改 get 返回的数据不等于直接修改磁盘文件。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    one = runtime.get(run.id)
    one.spec.criteria[0].config["argv"].append("modified")
    assert "modified" not in runtime.get(run.id).spec.criteria[0].config["argv"]


def test_lifecycle_to_verifying_without_false_success(tmp_path):
    """验证 Goal 只能从 pending 进入执行和待验收，不能直接标记为成功。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    run = runtime.begin_execution(run.id)
    assert run.status == "executing" and run.version == 1
    run = runtime.begin_verification(run.id)
    assert run.status == "verifying" and run.version == 2
    assert run.criteria["login_401"].status == "unverified"
    assert "verified" not in [event.to_status for event in run.events]


def test_invalid_transition_rejected(tmp_path):
    """验证未执行就验收以及重复开始执行都被拒绝。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    with pytest.raises(ValueError):
        runtime.begin_verification(run.id)
    runtime.begin_execution(run.id)
    with pytest.raises(ValueError):
        runtime.begin_execution(run.id)


def test_unknown_requires_reason(tmp_path):
    """验证结果未知时必须写明原因以便后续人工核实。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    runtime.begin_execution(run.id)
    with pytest.raises(ValueError, match="reason"):
        runtime.mark_unknown(run.id, "")
    unknown = runtime.mark_unknown(run.id, "远端任务超时，结果未知")
    assert unknown.status == "unknown"
    assert unknown.events[-1].note == "远端任务超时，结果未知"


def test_block_from_unknown_persists(tmp_path):
    """验证结果未知后可阻塞目标并持久化人工说明。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    runtime.begin_execution(run.id)
    runtime.mark_unknown(run.id, "CI 状态不确定")
    blocked = runtime.block(run.id, "等待人工检查 GitHub")
    assert blocked.status == "blocked"
    assert make_runtime(tmp_path).get(run.id).events[-1].note == "等待人工检查 GitHub"


def test_add_note_does_not_change_status(tmp_path):
    """验证记录宿主备注不会伪造任何验收结果。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    updated = runtime.add_note(run.id, "已收到 GitHub Issue #12")
    assert updated.status == "pending"
    assert updated.events[-1].event_type == "note"


def test_list_goals_supports_status_filter(tmp_path):
    """验证 UI 能枚举目标并筛选目前正在执行的目标。"""
    runtime = make_runtime(tmp_path)
    first = runtime.start(spec())
    second = runtime.start(spec())
    runtime.begin_execution(second.id)
    assert {run.id for run in runtime.list_goals()} == {first.id, second.id}
    assert [run.id for run in runtime.list_goals("executing")] == [second.id]


def test_stale_revision_does_not_overwrite(tmp_path):
    """验证过期版本的写入不能覆盖刚保存的更新。"""
    store = GoalStore(tmp_path / "goals")
    runtime = GoalRuntime(store)
    initial = runtime.start(spec())
    old_copy = store.get(initial.id)
    runtime.begin_execution(initial.id)
    old_copy.status = "blocked"
    with pytest.raises(GoalConflictError):
        store.save(old_copy)
    assert store.get(initial.id).status == "executing"


def test_run_id_prevents_file_traversal(tmp_path):
    """验证读取目标不能利用编号访问任意磁盘文件。"""
    store = GoalStore(tmp_path / "goals")
    with pytest.raises(ValueError):
        store.get("../../outside")


def test_corrupt_json_reports_error(tmp_path):
    """验证损坏的目标文件会显式报错而不是伪装成正常记录。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    (tmp_path / "goals" / f"{run.id}.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        runtime.get(run.id)


def test_verified_requires_real_evidence_even_at_storage_boundary(tmp_path):
    """验证试图直接把未验收目标写成 verified 会被 Store 拒绝。"""
    runtime = make_runtime(tmp_path)
    run = runtime.start(spec())
    altered = runtime.get(run.id)
    altered.status = "verified"
    with pytest.raises(ValueError, match="evidence-backed"):
        runtime.store.save(altered)


def test_bad_clock_rejected(tmp_path):
    """验证无时区时钟不会产生可能误读的审批或审计时间。"""
    runtime = GoalRuntime(GoalStore(tmp_path / "goals"), clock=lambda: datetime(2026, 10, 28, 12, 0))
    with pytest.raises(ValueError, match="timezone"):
        runtime.start(spec())
