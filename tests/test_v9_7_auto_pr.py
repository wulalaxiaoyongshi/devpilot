"""检验真实 Git 分支发布、安全审批和 GitHub PR 幂等行为的离线测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from devpilot.runtime import pr_publisher as publisher


RUN_ID = "cd17c3589f184c3b875760ae817aca3d"
ISSUE = "https://github.com/wulalaxiaoyongshi/devpilot-mcp-e2e-lab/issues/1"


def _git(repo: Path, *args: str) -> str:
    """执行无 Shell 的真实 Git 命令以构造独立测试仓库。"""
    return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()


def _make_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bad_code: bool = False) -> tuple[Path, Path, Path]:
    """创建真实 Git 裸远端和带有 Workflow、Goal 证据的本地修复会话。"""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(tmp_path, "init", "--bare", str(bare))
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "DevPilot Tester")
    _git(repo, "config", "user.email", "tester@example.invalid")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text("from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial buggy code")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b + 0\n" if bad_code else "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    _git(repo, "add", "calculator.py")
    _git(repo, "commit", "-m", "fix: add function")
    head = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    for folder in ("sessions", "workflows", "reports"):
        (state / folder).mkdir(parents=True)
    session = {"repo": str(repo.resolve()), "run_id": RUN_ID, "base_head": base,
               "test_mode": "pytest", "task": f"Please fix this GitHub Issue: {ISSUE}", "plan": {"workers": [{"id": "worker1", "files": ["calculator.py"]}]}}
    workflow = {"id": RUN_ID, "status": "completed", "steps": {
        "preapproval": {"status": "succeeded"}, "worker1": {"status": "succeeded"},
        "merge": {"status": "succeeded"}, "verify": {"status": "succeeded"},
        "delivery_review": {"status": "succeeded", "approval_request": {"status": "approved"}},
        "report": {"status": "succeeded"},
    }}
    report = {"run_id": RUN_ID, "status": "ready_for_delivery", "git_base": base,
              "git_head": head, "changed_files": ["calculator.py"],
              "worker_jobs": {"worker1": "job-001"},
              "goal": {"goal_status": "verified", "delivery_status": "ready_for_delivery",
                       "criteria": [{"required": True, "status": "pass"}]}}
    for folder, item in (("sessions", session), ("workflows", workflow), ("reports", report)):
        (state / folder / f"{RUN_ID}.json").write_text(json.dumps(item), encoding="utf-8")
    monkeypatch.setattr(publisher, "_assert_origin", lambda *args: None)
    return repo, state, bare


def _fake_gh(monkeypatch: pytest.MonkeyPatch, plan: publisher.PRPlan) -> dict:
    """用状态化 GitHub CLI 替身测试 PR 查询与创建而不访问真实网络。"""
    state = {"prs": [], "created": 0}

    def fake_gh(repo: Path, *args: str) -> str:
        """按真实 gh pr list/create 的 JSON 形状返回结果并记录副作用。"""
        assert args[0] == "pr"
        if args[1] == "list":
            return json.dumps(state["prs"])
        assert args[1] == "create"
        assert "--base" in args and args[args.index("--base") + 1] == "main"
        assert "--head" in args and args[args.index("--head") + 1] == plan.publish_branch
        state["created"] += 1
        state["prs"].append({"url": f"https://github.com/{plan.github_repo}/pull/1",
                             "state": "OPEN", "baseRefName": "main",
                             "headRefName": plan.publish_branch, "headRefOid": plan.head_sha})
        return state["prs"][-1]["url"]

    monkeypatch.setattr(publisher, "_gh", fake_gh)
    return state


def test_plan_reads_real_git_and_workflow_evidence(tmp_path, monkeypatch):
    """必须先核对真实 Git 版本、V7 审批与 V8 验收才能生成待发布计划。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    assert plan.github_repo == "wulalaxiaoyongshi/devpilot-mcp-e2e-lab"
    assert plan.publish_branch.startswith("devpilot/issue-1-")
    assert plan.changed_files == ("calculator.py",)
    assert plan.base_sha == _git(repo, "ls-remote", "--heads", "origin", "refs/heads/main").split()[0]


def test_publish_requires_explicit_human_confirmation(tmp_path, monkeypatch):
    """没有真人确认口令时任何远程修复分支都不能出现。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    with pytest.raises(PermissionError):
        publisher.publish_pr(plan, state, lambda _: False)
    assert not _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{plan.publish_branch}")


def test_publish_creates_pr_without_touching_remote_main(tmp_path, monkeypatch):
    """真实 Git 推送只能创建修复分支，GitHub PR 必须指向 main 而不能合并。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    gh = _fake_gh(monkeypatch, plan)
    result = publisher.publish_pr(plan, state, lambda _: True)
    assert result["passed"] and not result["merged"]
    assert gh["created"] == 1
    assert _git(repo, "ls-remote", "--heads", "origin", "refs/heads/main").split()[0] == plan.base_sha
    assert _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{plan.publish_branch}").split()[0] == plan.head_sha
    assert (state / "github_pr" / f"{RUN_ID}.json").is_file()


def test_publish_is_idempotent_when_pr_already_exists(tmp_path, monkeypatch):
    """相同任务重复发布必须复用同一个 GitHub PR 而不是再次创建。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    gh = _fake_gh(monkeypatch, plan)
    first = publisher.publish_pr(plan, state, lambda _: True)
    second = publisher.publish_pr(plan, state, lambda _: True)
    assert first["pr_url"] == second["pr_url"]
    assert second["reused"] is True
    assert gh["created"] == 1


def test_workflow_missing_review_cannot_publish(tmp_path, monkeypatch):
    """V7 最终审批状态不是 approved 时不能构建远程发布计划。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    path = state / "workflows" / f"{RUN_ID}.json"
    obj = json.loads(path.read_text())
    obj["steps"]["delivery_review"]["approval_request"]["status"] = "pending"
    path.write_text(json.dumps(obj))
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_goal_not_verified_cannot_publish(tmp_path, monkeypatch):
    """V8 Goal 未 verified 时不得发布未经证据支持的 PR。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    path = state / "reports" / f"{RUN_ID}.json"
    obj = json.loads(path.read_text())
    obj["goal"]["goal_status"] = "failed"
    path.write_text(json.dumps(obj))
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_changed_head_cannot_publish(tmp_path, monkeypatch):
    """已验收后新增 Git 提交使当前 HEAD 改变时必须停止发布。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b + 1\n")
    _git(repo, "add", "calculator.py")
    _git(repo, "commit", "-m", "post-verification changes")
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_remote_main_advance_blocks_publication(tmp_path, monkeypatch):
    """远程 main 与修复开始时的基准不一致时必须请求人工重新集成。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_failed_real_pytest_cannot_push(tmp_path, monkeypatch):
    """即使历史报告声称通过，发布前真实 pytest 失败也不能推送。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch, bad_code=True)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    gh = _fake_gh(monkeypatch, plan)
    with pytest.raises(RuntimeError):
        publisher.publish_pr(plan, state, lambda _: True)
    assert gh["created"] == 0
    assert not _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{plan.publish_branch}")


def test_origin_mismatch_and_invalid_issue_url_rejected(tmp_path, monkeypatch):
    """不能把其他仓库的 Issue 当作目标或接受伪造 GitHub URL。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    monkeypatch.undo()
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    with pytest.raises(ValueError):
        publisher._issue_identity("https://github.com.evil.tld/u/r/issues/1")


def test_task_must_reference_the_original_issue(tmp_path, monkeypatch):
    """用户不能把已经验收的其他开发任务错误关联到新的 Issue。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    path = state / "sessions" / f"{RUN_ID}.json"
    payload = json.loads(path.read_text())
    payload["task"] = "Unrelated coding task"
    path.write_text(json.dumps(payload))
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_required_goal_criterion_must_pass(tmp_path, monkeypatch):
    """V8 任意必要验收条件失败时，即使总体状态写着 verified 也必须拒绝发布。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    path = state / "reports" / f"{RUN_ID}.json"
    payload = json.loads(path.read_text())
    payload["goal"]["criteria"][0]["status"] = "fail"
    path.write_text(json.dumps(payload))
    with pytest.raises(PermissionError):
        publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)


def test_existing_remote_branch_with_different_commit_rejected(tmp_path, monkeypatch):
    """远端已有同名分支但 SHA 不同，不能强推覆盖其代码。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    gh = _fake_gh(monkeypatch, plan)
    _git(repo, "push", "origin", f"{plan.base_sha}:refs/heads/{plan.publish_branch}")
    with pytest.raises(RuntimeError):
        publisher.publish_pr(plan, state, lambda _: True)
    assert gh["created"] == 0


def test_closed_github_pr_is_not_silently_recreated(tmp_path, monkeypatch):
    """同一提交的旧 PR 已关闭时拒绝生成第二张重复 PR。"""
    repo, state, bare = _make_fixture(tmp_path, monkeypatch)
    plan = publisher.build_pr_plan(repo, state, RUN_ID, ISSUE)
    gh = _fake_gh(monkeypatch, plan)
    gh["prs"].append({"url": f"https://github.com/{plan.github_repo}/pull/1", "state": "CLOSED",
                      "baseRefName": "main", "headRefName": plan.publish_branch, "headRefOid": plan.head_sha})
    with pytest.raises(RuntimeError):
        publisher.publish_pr(plan, state, lambda _: True)
    assert gh["created"] == 0
