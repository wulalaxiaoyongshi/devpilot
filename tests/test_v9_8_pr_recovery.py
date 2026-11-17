"""V9.8 PR Recovery：真实本地 Git、真实 V7 Store、可信 GitHub API 替身的安全回归。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from devpilot.runtime import pr_publisher
from devpilot.runtime.operation_guard import OperationLedger
from devpilot.runtime.pr_recovery import inspect_recovery, recover_pr, _recovery_journal
from devpilot.runtime.general_plan import build_general_workflow
from devpilot.runtime.workflow_recipes import with_remote_delivery
from devpilot.workflow.approval import ApprovalRequest
from devpilot.workflow.engine import WorkflowEngine
from devpilot.workflow.actions import ActionRegistry
from devpilot.workflow.store import WorkflowStore


def _git(repo: Path, *args: str) -> str:
    """以真实 Git CLI 执行测试仓库操作，不依赖网络。"""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, encoding="utf-8",
                            errors="replace", text=True, check=True)
    return result.stdout.strip()


class FakeGitHub:
    """仅在测试中模拟 gh API 和创建 PR，保留真实裸远端与 Git Push。"""

    def __init__(self, repo: Path):
        """保存本地裸远端的查询状态和调用次数。"""
        self.repo = repo
        self.prs: list[dict] = []
        self.created = 0
        self.network_error_after_create = False
        self.network_error_before_create = False
        self.api_error = False

    def _sha(self, branch: str) -> str | None:
        """通过真实 Git 协议从本地裸远端获取精确分支提交。"""
        output = _git(self.repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        return output.split()[0] if output else None

    def add_pr(self, branch: str, head_sha: str, *, merged=False, closed=False):
        """构造符合 GitHub REST 响应格式的同仓库 PR 元数据。"""
        number = len(self.prs) + 4
        pr = {"html_url": f"https://github.com/demo/lab/pull/{number}", "number": number,
              "state": "closed" if closed or merged else "open", "merged_at": "2026-11-13T00:00:00Z" if merged else None,
              "head": {"ref": branch, "sha": head_sha, "repo": {"full_name": "demo/lab"}},
              "base": {"ref": "main", "repo": {"full_name": "demo/lab"}}}
        self.prs.append(pr)
        return pr

    def __call__(self, _repo: Path, *args: str) -> str:
        """响应匹配 refs、按 head 过滤 pulls 和 create 请求，拒绝所有其他操作。"""
        if self.api_error:
            raise RuntimeError("GitHub API authentication or transport error")
        if args[:2] == ("api", "--method"):
            assert args[2:4] == ("GET", "repos/demo/lab/pulls")
            head = next(item[5:] for item in args if item.startswith("head="))
            return json.dumps([p for p in self.prs if "demo:" + p["head"]["ref"] == head])
        if args[:2] == ("api", "repos/demo/lab/git/matching-refs/heads/main") or (len(args) == 2 and args[0] == "api" and args[1].startswith("repos/demo/lab/git/matching-refs/heads/")):
            branch = args[1].split("/heads/", 1)[1]
            sha = self._sha(branch)
            return json.dumps([{"ref": f"refs/heads/{branch}", "object": {"sha": sha}}] if sha else [])
        if args[:2] == ("pr", "create"):
            self.created += 1
            if self.network_error_before_create:
                raise RuntimeError("Connection reset before response")
            branch = args[args.index("--head") + 1]
            sha = self._sha(branch)
            assert sha is not None
            result = self.add_pr(branch, sha)
            if self.network_error_after_create:
                raise RuntimeError("Connection reset after GitHub accepted PR")
            return result["html_url"]
        raise AssertionError("Unexpected GitHub CLI command: " + repr(args))


@pytest.fixture
def site(tmp_path: Path, monkeypatch):
    """创建真实 Git 提交、独立裸远端与完整 V7/V8 交付证据。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mathbox.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_mathbox.py").write_text("from mathbox import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "DevPilot Tester")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "switch", "-C", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Bug")
    base = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    (repo / "mathbox.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "mathbox.py")
    _git(repo, "commit", "-m", "Fixed")
    head = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    store = WorkflowStore(state / "workflows")
    plan = {"summary": "fix", "workers": [{"id": "worker1", "task": "fix", "files": ["mathbox.py"]}]}
    definition = with_remote_delivery(build_general_workflow(plan, workflow_id="coding_test", approver="reviewer"),
                                      approver="reviewer")
    workflow = WorkflowEngine(store, ActionRegistry()).start(definition, {"goal_run_id": "goal-id"})
    run = store.get(workflow.id)
    for spec in definition.steps:
        step = run.steps[spec.id]
        step.status = "succeeded" if spec.id != "publish_pr" else "failed"
        if spec.kind == "approval":
            step.approval_request = ApprovalRequest("review-request", "approved", "2026-01-01T00:00:00+00:00",
                                                     decided_by="reviewer")
        if spec.id == "worker1":
            step.output = {"ok": True, "job_id": "job-001", "worker": "worker1"}
        if spec.id == "publish_pr":
            step.attempts = 1
            step.error = "RuntimeError: Git connection failed"
    run.status = "failed"
    store.save(run)
    issue = "https://github.com/demo/lab/issues/3"
    session = {"run_id": run.id, "repo": str(repo.resolve()), "base_head": base,
               "task": "Fix " + issue, "plan": plan, "goal_id": "goal-id",
               "mode": "edit", "delivery": "github-pr", "issue_url": issue,
               "test_mode": "pytest", "approver": "reviewer"}
    report = {"run_id": run.id, "status": "ready_for_delivery", "git_base": base, "git_head": head,
              "changed_files": ["mathbox.py"], "worker_jobs": {"worker1": "job-001"},
              "goal": {"goal_status": "verified", "delivery_status": "ready_for_delivery",
                       "criteria": [{"id": "tests", "required": True, "status": "pass"}]}}
    for folder in ("sessions", "reports"):
        (state / folder).mkdir(exist_ok=True, parents=True)
    (state / "sessions" / f"{run.id}.json").write_text(json.dumps(session), encoding="utf-8")
    (state / "reports" / f"{run.id}.json").write_text(json.dumps(report), encoding="utf-8")
    (state / "reports" / f"{run.id}.md").write_text("# Validated delivery\n", encoding="utf-8")
    fake = FakeGitHub(repo)
    monkeypatch.setattr(pr_publisher, "_gh", fake)
    monkeypatch.setattr(pr_publisher, "_assert_origin", lambda *_: None)  # 真实 URL 在主代码校验，此处临时裸仓库并非 github.com
    return {"repo": repo, "state": state, "run": run.id, "base": base, "head": head,
            "issue": issue, "branch": f"devpilot/issue-3-{run.id[:8]}", "fake": fake, "store": store}


def inspect(site):
    """用当前真实本地状态和测试 GitHub 服务执行只读恢复诊断。"""
    return inspect_recovery(site["repo"], site["state"], site["run"])


def recover(site):
    """执行测试实例的恢复，仍采用真实 Git push 与真实 V7 reconcile_step。"""
    return recover_pr(site["repo"], site["state"], site["run"])


def test_existing_manual_pr_reconciles_original_failed_v7(site):
    """用户手工创建的 PR 必须只核对、补证据并将原 Workflow 变成 completed。"""
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    site["fake"].add_pr(site["branch"], site["head"])
    before = inspect(site)
    assert before.action == "reconcile_only"
    assert before.as_dict()["will_push"] is False
    result = recover(site)
    assert result["passed"] and result["workflow"] == "completed" and result["worker_rerun"] is False
    assert result["pr_url"].endswith("/pull/4")
    events = site["store"].get(site["run"]).steps["publish_pr"].recovery_events
    assert any(event.event == "original_failure_observed" and "connection failed" in event.evidence.lower()
               for event in events)
    assert site["store"].get(site["run"]).steps["publish_pr"].attempts == 1
    assert site["fake"].created == 0
    report = json.loads((site["state"] / "reports" / f"{site['run']}.json").read_text(encoding="utf-8"))
    assert report["github_pr"]["head_sha"] == site["head"]
    assert inspect(site).action == "reconcile_only"
    second = recover(site)
    assert second["passed"] and site["fake"].created == 0


def test_missing_branch_creates_only_verified_branch_and_pr(site):
    """远程没有分支和 PR 时，只执行一次真实 Git push，再创建 PR，不推送 main。"""
    assert inspect(site).action == "push_then_pr"
    result = recover(site)
    assert result["workflow"] == "completed"
    assert site["fake"].created == 1
    assert site["fake"]._sha("main") == site["base"]
    assert site["fake"]._sha(site["branch"]) == site["head"]
    assert _recovery_journal(site["state"], site["run"]).exists()


def test_branch_exists_but_pr_missing_creates_pr_without_push(site, monkeypatch):
    """已推送的正确分支只创建 PR，禁止重复推送。"""
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    assert inspect(site).action == "create_pr"
    def no_push(*_args, **_kwargs):
        """验证分支已有时绝对不能执行 push。"""
        raise AssertionError("unexpected git push")
    monkeypatch.setattr("devpilot.runtime.pr_recovery._push_verified_branch", no_push)
    assert recover(site)["passed"]
    assert site["fake"].created == 1


def test_pr_response_lost_but_remote_exists_is_confirmed(site):
    """GitHub 已接收 PR 但回执丢失，重新查询确认证据后不重复创建。"""
    site["fake"].network_error_after_create = True
    assert recover(site)["workflow"] == "completed"
    assert site["fake"].created == 1


def test_unconfirmed_pr_create_blocks_automatic_replay(site):
    """PR 创建响应丢失且 API 未确认时，记录未知状态并阻断自动重复提交。"""
    site["fake"].network_error_before_create = True
    with pytest.raises(RuntimeError, match="不确定"):
        recover(site)
    assert site["fake"].created == 1
    assert inspect(site).action == "uncertain_pr_create"
    with pytest.raises(PermissionError, match="uncertain_pr_create"):
        recover(site)
    assert site["fake"].created == 1
    assert site["store"].get(site["run"]).status == "failed"


def test_changed_main_blocks_new_remote_write(site):
    """远程 main 前移且 PR 不存在时，阻止继续创建 PR。"""
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/main")
    assert inspect(site).action == "conflict_main"
    with pytest.raises(PermissionError):
        recover(site)


def test_existing_pr_can_be_reconciled_after_main_advanced(site):
    """PR 已经存在后即使 main 前移，仍可核对历史提交并修复旧 Workflow 状态。"""
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    site["fake"].add_pr(site["branch"], site["head"])
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/main")
    assert recover(site)["workflow"] == "completed"


def test_remote_branch_conflict_does_not_force_push(site):
    """同名远程分支指向其他 SHA 时，恢复必须停止且不得覆盖。"""
    _git(site["repo"], "push", "origin", site["base"] + ":refs/heads/" + site["branch"])
    assert inspect(site).action == "conflict_branch"
    with pytest.raises(PermissionError):
        recover(site)
    assert site["fake"]._sha(site["branch"]) == site["base"]


def test_missing_publish_approval_is_rejected_before_network(site):
    """即使远端 PR 存在，没有 V7 publish_review 审批也不能将原任务标成成功。"""
    run = site["store"].get(site["run"])
    run.steps["publish_review"].approval_request = None
    site["store"].save(run)
    with pytest.raises(PermissionError):
        inspect(site)


def test_stale_local_head_fails_closed(site):
    """本地 HEAD 或实际提交与 V8 报告不一致时，拒绝 PR 恢复。"""
    _git(site["repo"], "reset", "--hard", site["base"])
    with pytest.raises(PermissionError, match="HEAD"):
        inspect(site)


def test_dirty_worktree_fails_closed(site):
    """工作区含未提交修改时，恢复拒绝沿用旧 Goal 证据。"""
    (site["repo"] / "mathbox.py").write_text("malicious local edits", encoding="utf-8")
    with pytest.raises(PermissionError, match="不干净"):
        inspect(site)


def test_goal_verification_failed_blocks(site):
    """V8 Goal 未 verified 或验收条件未 pass 时，不能通过恢复绕过 Goal。"""
    path = site["state"] / "reports" / f"{site['run']}.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["goal"]["goal_status"] = "verifying"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(PermissionError, match="Goal"):
        inspect(site)


def test_operation_ledger_inflight_reconciled_only_after_remote_pr(site):
    """V9.5 已提交但结果未知的操作，只能用真实远端 PR 证据核实为成功。"""
    ledger = OperationLedger(site["state"] / "operations")
    key = site["run"] + ":publish_pr"
    ledger.begin(key, description="publisher submit")
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    site["fake"].add_pr(site["branch"], site["head"])
    assert recover(site)["passed"]
    assert ledger.get(key)["status"] == "succeeded"


def test_api_network_failure_never_treated_as_missing(site):
    """GitHub API 网络失败不得误判为缺失远程 PR 或分支。"""
    site["fake"].api_error = True
    with pytest.raises(RuntimeError, match="transport"):
        inspect(site)
    assert site["store"].get(site["run"]).status == "failed"


def test_closed_unmerged_pr_cannot_be_marked_delivered(site):
    """已关闭但未合并的 PR 不构成成功交付，防止错误状态回填。"""
    site["fake"].add_pr(site["branch"], site["head"], closed=True)
    with pytest.raises(PermissionError, match="关闭"):
        inspect(site)


def test_already_merged_pr_reconciles_without_merging_again(site):
    """旧 PR 已由维护者合并且远程分支删除，也能只读核实并正确记 merged。"""
    site["fake"].add_pr(site["branch"], site["head"], merged=True)
    result = recover(site)
    assert result["merged"] is True and result["workflow"] == "completed"
    assert site["fake"].created == 0


def test_mismatched_pr_head_blocks(site):
    """PR 分支名相同但 head SHA 与原 Goal 报告不同，必须终止。"""
    site["fake"].add_pr(site["branch"], site["base"])
    with pytest.raises(PermissionError):
        inspect(site)


def test_stale_lock_blocks_second_recovery(site):
    """排他锁存在时禁止另一个恢复进程同时操作远端。"""
    lock = site["state"] / "pr_recovery" / (site["run"] + ".lock")
    lock.parent.mkdir(parents=True)
    lock.write_text("another process", encoding="utf-8")
    with pytest.raises(RuntimeError, match="锁"):
        recover(site)


def test_issue_identity_mismatch_blocks(site):
    """不能把其它 Issue 的 PR 挂到当前已审批的开发任务。"""
    with pytest.raises(PermissionError, match="Issue"):
        inspect_recovery(site["repo"], site["state"], site["run"], issue_url="https://github.com/demo/lab/issues/99")


def test_original_inflight_without_remote_confirmation_is_blocked(site):
    """原 V9.5 OperationLedger 有 inflight 却没有 PR 时，不允许自动再次发布。"""
    ledger = OperationLedger(site["state"] / "operations")
    ledger.begin(site["run"] + ":publish_pr", description="original publish possibly sent")
    assert inspect(site).action == "uncertain_original_publish"
    with pytest.raises(PermissionError):
        recover(site)
    assert site["fake"].created == 0


def test_receipt_exists_but_remote_pr_missing_is_conflict(site):
    """保存过发布成功回执而 GitHub 查不到 PR 时，不允许静默重建。"""
    path = site["state"] / "github_pr" / f"{site['run']}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pr_url": "https://github.com/demo/lab/pull/4",
                                "head_sha": site["head"], "branch": site["branch"],
                                "issue_url": site["issue"]}), encoding="utf-8")
    assert inspect(site).action == "missing_published_pr"
    with pytest.raises(PermissionError):
        recover(site)


def test_confirmed_branch_missing_is_not_silently_repushed(site):
    """曾记录成功推送但远端分支已消失，需人工解释，不能自动推新分支。"""
    journal = _recovery_journal(site["state"], site["run"])
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"head_sha": site["head"], "branch": site["branch"],
                                   "issue_url": site["issue"], "phase": "push_confirmed"}), encoding="utf-8")
    assert inspect(site).action == "missing_confirmed_branch"
    with pytest.raises(PermissionError):
        recover(site)


def test_cli_dry_run_is_readonly(site, capsys):
    """recover-pr --dry-run 仅检查 GitHub 实际状态，绝不推进 V7 或推送分支。"""
    from devpilot.cli import main
    code = main(["recover-pr", "--repo", str(site["repo"]), "--state", str(site["state"]),
                 "--run-id", site["run"], "--dry-run"])
    assert code == 0
    output = capsys.readouterr().out
    assert '"action": "push_then_pr"' in output
    assert site["store"].get(site["run"]).status == "failed"
    assert site["fake"].created == 0
    assert site["fake"]._sha(site["branch"]) is None


def test_cli_can_reconcile_manually_created_pr_without_model(site, monkeypatch, capsys):
    """恢复已存在 PR 只需输入 RECOVER 口令，CLI 不初始化或调用 DeepSeek。"""
    from devpilot.cli import main
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    site["fake"].add_pr(site["branch"], site["head"])
    monkeypatch.setattr("builtins.input", lambda prompt: "RECOVER " + site["run"])
    result = main(["recover-pr", "--repo", str(site["repo"]), "--state", str(site["state"]),
                   "--run-id", site["run"], "--git-http1.1"])
    assert result == 0
    assert site["store"].get(site["run"]).status == "completed"
    assert '"worker_rerun": false' in capsys.readouterr().out


def test_cli_wrong_confirmation_does_not_modify_remote(site, monkeypatch):
    """恢复口令错误时终止，不运行 Git Push 或 gh pr create。"""
    from devpilot.cli import main
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["recover-pr", "--repo", str(site["repo"]), "--state", str(site["state"]),
                 "--run-id", site["run"]]) == 2
    assert site["fake"]._sha(site["branch"]) is None
    assert site["store"].get(site["run"]).status == "failed"


def test_old_not_executed_ledger_keeps_history_when_recovery_publishes(site):
    """原发布已确认未执行时，Recovery 新建 PR 后不得篡改原操作的历史结果。"""
    ledger = OperationLedger(site["state"] / "operations")
    key = site["run"] + ":publish_pr"
    ledger.begin(key, description="original submit")
    ledger.reconcile(key, status="not_executed", actor="reviewer", evidence="GitHub observed absent")
    assert recover(site)["workflow"] == "completed"
    assert ledger.get(key)["status"] == "not_executed"


def test_saved_report_with_missing_remote_pr_is_blocked(site):
    """报告已记 PR 而远端 PR 丢失时，不得忽略历史证据重新发布。"""
    path = site["state"] / "reports" / f"{site['run']}.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["github_pr"] = {"url": "https://github.com/demo/lab/pull/4",
                           "branch": site["branch"], "head_sha": site["head"]}
    path.write_text(json.dumps(report), encoding="utf-8")
    assert inspect(site).action == "missing_published_pr"
    with pytest.raises(PermissionError):
        recover(site)


def test_recovery_required_step_uses_native_v7_reconciliation(site):
    """V7 原生 recovery_required 状态通过同一个 reconcile_step 核实远端完成。"""
    run = site["store"].get(site["run"])
    run.steps["publish_pr"].status = "recovery_required"
    run.status = "recovery_required"
    site["store"].save(run)
    _git(site["repo"], "push", "origin", site["head"] + ":refs/heads/" + site["branch"])
    site["fake"].add_pr(site["branch"], site["head"])
    assert recover(site)["workflow"] == "completed"
    assert any(row.event == "reconciled_succeeded" for row in site["store"].get(site["run"]).steps["publish_pr"].recovery_events)


def test_http11_option_only_changes_this_git_push(site):
    """HTTP/1.1 仅用于恢复时的 Git push，不会更改系统或仓库全局 Git 配置。"""
    assert recover_pr(site["repo"], site["state"], site["run"], http1_1=True)["workflow"] == "completed"
    assert site["fake"]._sha("main") == site["base"]
    assert site["fake"]._sha(site["branch"]) == site["head"]


def test_open_pr_with_missing_remote_branch_requires_human_check(site):
    """OPEN PR 若其源分支消失则拒绝在原 Workflow 写入错误成功状态。"""
    site["fake"].add_pr(site["branch"], site["head"])
    with pytest.raises(PermissionError, match="源分支不存在"):
        inspect(site)


def test_concurrent_remote_branch_creation_does_not_move_it(site, monkeypatch):
    """其他人刚创建同名远端分支时，期望空引用的 lease 禁止覆盖其 Git SHA。"""
    actual_git = pr_publisher._git
    count = {"seen": 0}

    def raced_git(repo, *args):
        """在第一次 Recovery Push 之前模拟另一位开发者创建同名分支。"""
        if "push" in args and "--force-with-lease=refs/heads/" in " ".join(args) and count["seen"] == 0:
            count["seen"] = 1
            _git(site["repo"], "push", "origin", site["base"] + ":refs/heads/" + site["branch"])
        return actual_git(repo, *args)

    monkeypatch.setattr(pr_publisher, "_git", raced_git)
    with pytest.raises(RuntimeError, match="停止操作"):
        recover(site)
    assert site["fake"]._sha(site["branch"]) == site["base"]
    assert site["store"].get(site["run"]).status == "failed"
