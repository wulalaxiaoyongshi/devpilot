"""V9.8 PR Recovery：以真实 GitHub 远端证据恢复失败发布，不重新执行开发步骤。"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from devpilot.runtime import pr_publisher as publisher
from devpilot.runtime.operation_guard import OperationLedger
from devpilot.workflow.actions import ActionRegistry
from devpilot.workflow.engine import WorkflowEngine
from devpilot.workflow.store import WorkflowStore


@dataclass(frozen=True)
class RecoveryPreview:
    """持有经过 V7/V8 验证的 PR 计划以及 GitHub API 的当前观测结果。"""

    plan: publisher.PRPlan
    action: str
    remote_main: str
    remote_head: str | None
    pr_url: str | None
    pr_state: str | None
    issue_url: str
    journal_phase: str | None

    def as_dict(self) -> dict[str, Any]:
        """输出不包含 Token 的预览，供操作者决定是否恢复远程发布。"""
        return {"run_id": self.plan.run_id, "github_repo": self.plan.github_repo,
                "issue_url": self.issue_url, "base_sha": self.plan.base_sha, "head_sha": self.plan.head_sha,
                "publish_branch": self.plan.publish_branch, "remote_main": self.remote_main,
                "remote_branch_sha": self.remote_head, "pr_url": self.pr_url,
                "pr_state": self.pr_state, "action": self.action, "journal_phase": self.journal_phase,
                "will_merge_main": False,
                "will_push": self.action == "push_then_pr",
                "will_create_pr": self.action in {"push_then_pr", "create_pr"}}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """以 UTF-8 原子替换保存可信恢复状态，支持崩溃后再次读取。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _recovery_journal(state: Path, run_id: str) -> Path:
    """在仓库外为每条 Workflow 指定单一恢复记录路径。"""
    return state / "pr_recovery" / f"{run_id}.json"


def _write_journal(state: Path, plan: publisher.PRPlan, phase: str, **extra: Any) -> None:
    """记录远程操作前后阶段，使未知响应不会被下一次运行自动重放。"""
    path = _recovery_journal(state, plan.run_id)
    history = publisher._read_json(path) if path.is_file() else {}
    if history and (history.get("head_sha") != plan.head_sha or
                    history.get("branch") != plan.publish_branch or
                    history.get("issue_url") != plan.issue_url):
        raise RuntimeError("恢复记录与本次任务不同，禁止覆盖")
    history.update({"run_id": plan.run_id, "head_sha": plan.head_sha,
                    "branch": plan.publish_branch, "issue_url": plan.issue_url,
                    "phase": phase, "updated_at": datetime.now(timezone.utc).isoformat()})
    history.update(extra)
    _atomic_json(path, history)


@contextmanager
def _exclusive_recovery(state: Path, run_id: str) -> Iterator[None]:
    """对同一 Run 创建排他文件锁，禁止两个恢复进程同时执行 GitHub 写入。"""
    directory = state / "pr_recovery"
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / f"{run_id}.lock"
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise RuntimeError(f"检测到现有 PR Recovery 锁：{lock}；先核实是否有其他进程或上次崩溃") from error
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _remote_ref(plan: publisher.PRPlan, branch: str) -> str | None:
    """用已认证 gh API 查询精确 Git ref，避免将 Git 网络失败误当作分支不存在。"""
    if not publisher._ALLOWED_BRANCH.fullmatch(branch) or ".." in branch or "//" in branch:
        raise ValueError("Invalid Git branch")
    url = f"repos/{plan.github_repo}/git/matching-refs/heads/{branch}"
    response = json.loads(publisher._gh(Path(plan.repo), "api", url))
    if not isinstance(response, list):
        raise RuntimeError("GitHub API 未返回 refs 数组，无法确认远程状态")
    matched = [ref for ref in response if isinstance(ref, dict) and ref.get("ref") == f"refs/heads/{branch}"]
    if len(matched) > 1:
        raise RuntimeError("GitHub ref 返回多个相同条目，停止操作")
    if not matched:
        return None
    sha = (matched[0].get("object") or {}).get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("远程分支 SHA 不合法")
    return sha


def _query_pr(plan: publisher.PRPlan) -> dict[str, Any] | None:
    """按 owner:branch 过滤 GitHub PR，并逐项校验仓库、源提交及 main 目标。"""
    owner = plan.github_repo.split("/", 1)[0]
    args = ("api", "--method", "GET", f"repos/{plan.github_repo}/pulls",
            "-f", f"head={owner}:{plan.publish_branch}", "-f", "state=all", "-f", "per_page=100")
    result = json.loads(publisher._gh(Path(plan.repo), *args))
    if not isinstance(result, list):
        raise RuntimeError("GitHub API 未返回 PR 数组，停止操作")
    if len(result) > 1:
        raise PermissionError("同一发布分支匹配多个 PR，需要人工核实")
    if not result:
        return None
    pr = result[0]
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub PR 元数据格式不正确")
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    expected = plan.github_repo.lower()
    head_repo = (head.get("repo") or {}).get("full_name", "").lower()
    base_repo = (base.get("repo") or {}).get("full_name", "").lower()
    url = pr.get("html_url")
    number = pr.get("number")
    if (head_repo != expected or base_repo != expected or head.get("ref") != plan.publish_branch or
            head.get("sha") != plan.head_sha or base.get("ref") != plan.base_branch or
            not isinstance(number, int) or number < 1 or
            url != f"https://github.com/{plan.github_repo}/pull/{number}"):
        raise PermissionError("GitHub PR 的仓库、分支或提交与 V8 已验收记录不一致")
    if pr.get("state") not in {"open", "closed"}:
        raise RuntimeError("GitHub PR 状态未知")
    if pr.get("state") == "closed" and not pr.get("merged_at"):
        raise PermissionError("原 PR 已关闭但未合并，不能把该任务标为交付成功")
    return {"url": url, "merged": bool(pr.get("merged_at")),
            "state": "MERGED" if pr.get("merged_at") else "OPEN", "number": number}


def inspect_recovery(repo: Path, state: Path, run_id: str, *, issue_url: str | None = None) -> RecoveryPreview:
    """只读核对本地 V7/V8 与 GitHub API，在未知结果时绝不猜测远端状态。"""
    if not publisher._RUN_ID.fullmatch(run_id):
        raise ValueError("无效的 run_id")
    repo = Path(repo).expanduser().resolve(strict=True)
    state = Path(state).expanduser().resolve(strict=True)
    session = publisher._read_json(state / "sessions" / f"{run_id}.json")
    saved_issue = session.get("issue_url")
    if not isinstance(saved_issue, str) or (issue_url is not None and saved_issue != issue_url):
        raise PermissionError("恢复 Issue URL 必须与原始会话完全相同")
    plan = publisher.build_pr_plan(repo, state, run_id, saved_issue, allow_recovery=True)
    journal_path = _recovery_journal(state, run_id)
    journal = publisher._read_json(journal_path) if journal_path.exists() else {}
    if journal and (journal.get("head_sha") != plan.head_sha or journal.get("branch") != plan.publish_branch
                    or journal.get("issue_url") != plan.issue_url):
        raise PermissionError("原恢复记录与 Git/Issue 证据不匹配")
    receipt_path = state / "github_pr" / f"{run_id}.json"
    old_receipt = publisher._read_json(receipt_path) if receipt_path.exists() else {}
    saved_report = publisher._read_json(state / "reports" / f"{run_id}.json")
    reported_pr = saved_report.get("github_pr") or {}
    if reported_pr and (reported_pr.get("head_sha") != plan.head_sha or
                        reported_pr.get("branch") != plan.publish_branch):
        raise PermissionError("已保存的交付报告包含冲突 PR 证据")
    if old_receipt and (old_receipt.get("head_sha") != plan.head_sha or
                        old_receipt.get("branch") != plan.publish_branch or
                        old_receipt.get("issue_url") != plan.issue_url):
        raise PermissionError("原 PR 回执与当前已验收 SHA、Issue 或分支不同")
    main_sha = _remote_ref(plan, plan.base_branch)
    if main_sha is None:
        raise RuntimeError("无法确认 GitHub main 分支；禁止恢复")
    remote_head = _remote_ref(plan, plan.publish_branch)
    pr = _query_pr(plan)
    original_ledger = OperationLedger(state / "operations").get(f"{run_id}:publish_pr")
    if pr is not None:
        if pr["state"] == "OPEN" and remote_head is None:
            raise PermissionError("PR 仍为 OPEN 但其远程源分支不存在，必须人工核查")
        if remote_head is not None and remote_head != plan.head_sha:
            raise PermissionError("PR 已创建但远程同名分支被移动，不能安全核实")
        if old_receipt and old_receipt.get("pr_url") != pr["url"]:
            raise PermissionError("已保存回执指向另一个 PR，拒绝覆盖")
        if reported_pr and reported_pr.get("url") != pr["url"]:
            raise PermissionError("已保存交付报告指向另一个 PR，拒绝覆盖")
        action = "reconcile_only"
    elif old_receipt or reported_pr or journal.get("phase") == "completed":
        action = "missing_published_pr"
    elif journal.get("phase") == "pr_create_requested":
        action = "uncertain_pr_create"
    elif journal.get("phase") == "push_requested" and remote_head is None:
        action = "uncertain_push"
    elif journal.get("phase") == "push_confirmed" and remote_head is None:
        action = "missing_confirmed_branch"
    elif original_ledger and original_ledger.get("status") == "inflight":
        action = "uncertain_original_publish"
    elif original_ledger and original_ledger.get("status") == "succeeded":
        action = "missing_published_pr"
    elif remote_head is not None and remote_head != plan.head_sha:
        action = "conflict_branch"
    elif main_sha != plan.base_sha:
        action = "conflict_main"
    elif remote_head is None:
        action = "push_then_pr"
    else:
        action = "create_pr"
    original_workflow = publisher._read_json(state / "workflows" / f"{run_id}.json")
    if original_workflow["steps"]["publish_pr"]["status"] == "succeeded" and pr is None:
        raise RuntimeError("本地记录发布成功但 GitHub 不存在匹配 PR，需要人工调查")
    return RecoveryPreview(plan, action, main_sha, remote_head,
                           pr["url"] if pr else None, pr["state"] if pr else None,
                           saved_issue, journal.get("phase"))


def _verify_before_write(preview: RecoveryPreview) -> None:
    """在任何远程写操作之前重新运行可信测试并核对 Git HEAD、工作区及 main 基准。"""
    plan = preview.plan
    publisher.rerun_tests(plan)
    if (publisher._git(Path(plan.repo), "rev-parse", "HEAD") != plan.head_sha or
            publisher._git(Path(plan.repo), "status", "--porcelain", "--untracked-files=all")):
        raise PermissionError("测试执行后本地 Git 证据发生变化")
    if _remote_ref(plan, "main") != plan.base_sha:
        raise PermissionError("发布前 GitHub main 已改变，必须重新集成与验收")


def _push_verified_branch(plan: publisher.PRPlan, state: Path, *, http1_1: bool) -> None:
    """使用期望空引用的 Git lease 原子创建新分支，禁止竞争条件下覆盖已有分支。"""
    _write_journal(state, plan, "push_requested")
    argv = ["-c", "http.version=HTTP/1.1"] if http1_1 else []
    try:
        publisher._git(Path(plan.repo), *argv, "push",
                       f"--force-with-lease=refs/heads/{plan.publish_branch}:",
                       "origin", f"{plan.head_sha}:refs/heads/{plan.publish_branch}")
    except Exception:
        if _remote_ref(plan, plan.publish_branch) != plan.head_sha:
            raise RuntimeError("Git push 响应不确定且远端未确认提交，停止操作；禁止盲目重试") from None
    if _remote_ref(plan, plan.publish_branch) != plan.head_sha:
        raise RuntimeError("Git push 后无法确认已验收的远端提交")
    _write_journal(state, plan, "push_confirmed")


def _create_verified_pr(plan: publisher.PRPlan, state: Path) -> dict[str, Any]:
    """创建一次 GitHub PR，响应丢失时按远端查询结果核实而不立即重复提交。"""
    if _remote_ref(plan, "main") != plan.base_sha:
        raise PermissionError("GitHub main 已更新，停止创建 PR")
    if _remote_ref(plan, plan.publish_branch) != plan.head_sha:
        raise PermissionError("远程修复分支不再指向已经验收的 SHA")
    existing = _query_pr(plan)
    if existing is not None:
        return existing
    _write_journal(state, plan, "pr_create_requested")
    try:
        publisher._gh(Path(plan.repo), "pr", "create", "--repo", plan.github_repo,
                      "--base", plan.base_branch, "--head", plan.publish_branch,
                      "--title", plan.title, "--body", plan.body)
    except Exception:
        found = _query_pr(plan)
        if found is None:
            raise RuntimeError("PR 创建响应不确定且远端没有确认，停止操作；请检查 GitHub 后执行只读核对") from None
        return found
    result = _query_pr(plan)
    if result is None:
        raise RuntimeError("GitHub 未确认 PR 创建结果，停止后续步骤")
    return result


def _record_delivered(state: Path, plan: publisher.PRPlan, pr: dict[str, Any]) -> str:
    """在 V7 状态变更前原子保存 PR 回执及报告，以便崩溃后安全补齐。"""
    report_path = state / "reports" / f"{plan.run_id}.json"
    report = publisher._read_json(report_path)
    previous = report.get("github_pr")
    if previous and (previous.get("url") != pr["url"] or previous.get("head_sha") != plan.head_sha):
        raise PermissionError("历史交付报告已记录其他 PR，禁止覆盖")
    receipt_path = state / "github_pr" / f"{plan.run_id}.json"
    old_receipt = publisher._read_json(receipt_path) if receipt_path.is_file() else {}
    if old_receipt and (old_receipt.get("pr_url") != pr["url"] or old_receipt.get("head_sha") != plan.head_sha):
        raise PermissionError("原发布回执与真实 PR 冲突")
    receipt = publisher._save_receipt(state, plan, pr["url"])
    report["github_pr"] = {"url": pr["url"], "branch": plan.publish_branch,
                           "head_sha": plan.head_sha, "merged": pr["merged"],
                           "recovered": True, "verified_via": "GitHub API"}
    _atomic_json(report_path, report)
    markdown = state / "reports" / f"{plan.run_id}.md"
    if markdown.is_file():
        content = markdown.read_text(encoding="utf-8")
        if pr["url"] not in content:
            with markdown.open("a", encoding="utf-8") as stream:
                stream.write("\nGitHub PR Recovery（远程已核实）：" + pr["url"] + "\n")
    return str(receipt)


def _reconcile_ledger(state: Path, plan: publisher.PRPlan, pr: dict[str, Any]) -> None:
    """仅在 GitHub 已确认 PR 后将原操作账本中的未知结果核实为成功。"""
    ledger = OperationLedger(state / "operations")
    key = f"{plan.run_id}:publish_pr"
    record = ledger.get(key)
    if record is None or record.get("status") in {"succeeded", "not_executed", "failed"}:
        return
    if record.get("status") != "inflight":
        raise PermissionError("原始 OperationLedger 状态未知，禁止改变 Workflow")
    ledger.reconcile(key, status="succeeded", actor="pr_recovery",
                     evidence=f"Confirmed GitHub PR {pr['url']} at {plan.head_sha}")


def _reconcile_v7(state: Path, plan: publisher.PRPlan, pr: dict[str, Any], receipt: str) -> str:
    """使用 V7 原生 reconcile_step 写入真实远端证据，使失败的发布步骤合法完成。"""
    store = WorkflowStore(state / "workflows")
    engine = WorkflowEngine(store, ActionRegistry())
    run = store.get(plan.run_id)
    step = run.steps["publish_pr"]
    result = {"ok": True, "passed": True, "pr_url": pr["url"], "receipt": receipt,
              "branch": plan.publish_branch, "head_sha": plan.head_sha,
              "recovered": True, "merged": pr["merged"]}
    if step.status == "succeeded":
        if not isinstance(step.output, dict) or step.output.get("pr_url") != pr["url"]:
            raise PermissionError("已完成 PR 步骤的输出与真实 GitHub PR 不一致")
        if run.status != "completed":
            raise RuntimeError("PR 步骤虽成功但整体工作流仍非 completed，需要人工检查")
        return run.status
    if step.status == "failed":
        with engine._lock:
            run = store.get(plan.run_id)
            if run.steps["publish_pr"].status != "failed":
                raise RuntimeError("PR 步骤在恢复时发生并发变更")
            prior_error = run.steps["publish_pr"].error or "Original PR failure not recorded"
            engine._record_recovery(run.steps["publish_pr"], "original_failure_observed",
                                    "pr_recovery", prior_error[:500])
            engine._needs_reconciliation(run.steps["publish_pr"], "PR exists on GitHub; confirm remote SHA")
            engine._save(run)
    updated = engine.reconcile_step(plan.run_id, "publish_pr", "succeeded", actor="pr_recovery",
                                    evidence=f"GitHub API confirmed {pr['url']} at {plan.head_sha}", output=result)
    if updated.status != "completed":
        raise RuntimeError("PR 已核实，但 V7 尚有其他未完成步骤；保留状态供人工检查")
    return updated.status


def recover_pr(repo: Path, state: Path, run_id: str, *, issue_url: str | None = None,
               http1_1: bool = False) -> dict[str, Any]:
    """持排他锁核对远端后完成缺失发布动作，再原生核实 V7 与账本，不重跑 Worker。"""
    state = Path(state).expanduser().resolve(strict=True)
    with _exclusive_recovery(state, run_id):
        preview = inspect_recovery(repo, state, run_id, issue_url=issue_url)
        if preview.action not in {"reconcile_only", "create_pr", "push_then_pr"}:
            raise PermissionError(f"恢复状态 {preview.action} 不允许远程写入；先人工核实冲突或未知结果")
        plan = preview.plan
        if preview.action == "reconcile_only":
            pr = _query_pr(plan)
            if pr is None:
                raise RuntimeError("核实 PR 时远端对象消失，停止恢复")
        else:
            _verify_before_write(preview)
            fresh = inspect_recovery(repo, state, run_id, issue_url=issue_url)
            if fresh.action == "reconcile_only":
                pr = _query_pr(plan)
            elif fresh.action in {"create_pr", "push_then_pr"}:
                if fresh.action == "push_then_pr":
                    _push_verified_branch(plan, state, http1_1=http1_1)
                pr = _create_verified_pr(plan, state)
            else:
                raise PermissionError(f"发布前远程状态已改变：{fresh.action}")
            if pr is None:
                raise RuntimeError("无法确认 GitHub PR 已存在")
        confirmed_pr = _query_pr(plan)
        if confirmed_pr is None or pr["url"] != confirmed_pr["url"]:
            raise RuntimeError("PR 在恢复过程中发生变化，拒绝记录结果")
        pr = confirmed_pr
        receipt = _record_delivered(state, plan, pr)
        _reconcile_ledger(state, plan, pr)
        workflow_status = _reconcile_v7(state, plan, pr, receipt)
        _write_journal(state, plan, "completed", pr_url=pr["url"], merged=pr["merged"])
        return {"passed": True, "run_id": run_id, "workflow": workflow_status,
                "pr_url": pr["url"], "pr_state": pr["state"], "merged": pr["merged"],
                "recovery_action": preview.action, "receipt": receipt, "worker_rerun": False,
                "main_merged_by_agent": False}
