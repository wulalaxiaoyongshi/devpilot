"""V9.7 后续：以已审批的真实 Git/Goal 证据安全发布 GitHub PR，不执行合并。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_ISSUE_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9]\d*)/?$")
_ALLOWED_BRANCH = re.compile(r"^[a-zA-Z0-9_/-]+$")


@dataclass(frozen=True)
class PRPlan:
    """保存已经通过本地工作流和 Git 版本校验的待发布 PR 信息。"""

    run_id: str
    repo: str
    github_repo: str
    issue_url: str
    issue_number: int
    base_branch: str
    base_sha: str
    head_sha: str
    publish_branch: str
    changed_files: tuple[str, ...]
    test_mode: str
    title: str
    body: str


def _process(argv: list[str], cwd: Path, timeout: int = 60) -> str:
    """用独立参数执行可信 Git/GitHub CLI 命令，并将输出固定解码为 UTF-8。"""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:400]
        for var in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "DEEPSEEK_API_KEY"):
            secret = os.environ.get(var)
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        raise RuntimeError(f"{argv[0]} 执行失败 (code={result.returncode}): {detail}")
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    """在指定仓库执行 Git 并返回标准输出，避免调用 shell。"""
    return _process(["git", *args], repo)


def _gh(repo: Path, *args: str) -> str:
    """通过当前 GitHub CLI 登录身份执行 GitHub 操作。"""
    return _process(["gh", *args], repo)


def _issue_identity(url: str) -> tuple[str, str, int]:
    """严格解析 GitHub Issue URL，拒绝非 GitHub 域名和伪造路径。"""
    parsed = urlparse(url)
    match = _ISSUE_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or match is None or parsed.query or parsed.fragment:
        raise ValueError("只接受 https://github.com/owner/repo/issues/N 格式")
    owner, name, number = match.groups()
    return owner, name, int(number)


def _assert_origin(repo: Path, owner: str, name: str) -> None:
    """确保将被推送的 origin 属于 Issue 对应的 GitHub 仓库。"""
    remote = _git(repo, "remote", "get-url", "origin").rstrip("/").removesuffix(".git").lower()
    suffix = f"{owner.lower()}/{name.lower()}"
    if remote not in {f"https://github.com/{suffix}", f"git@github.com:{suffix}", f"ssh://git@github.com/{suffix}"}:
        raise PermissionError("Git origin 与 Issue 仓库不一致，禁止推送")


def _read_json(path: Path) -> dict[str, Any]:
    """读取持久化 JSON 证据并确认顶层是对象。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"状态文件格式错误: {path.name}")
    return value


def _remote_sha(repo: Path, branch: str) -> str | None:
    """查询 GitHub 远程分支 SHA，避免覆盖其他人的提交。"""
    if not _ALLOWED_BRANCH.fullmatch(branch) or branch.startswith("/") or ".." in branch or "//" in branch:
        raise ValueError("分支名称不合法")
    output = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if not output:
        return None
    rows = [row.split() for row in output.splitlines() if row.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f"refs/heads/{branch}":
        raise RuntimeError("远程分支返回结果不唯一")
    return rows[0][0]


def _git_changes(repo: Path, base_sha: str, head_sha: str) -> tuple[str, ...]:
    """重新计算 Git 提交范围，检查实际改动文件与交付报告一致。"""
    output = _git(repo, "diff", "--name-only", base_sha, head_sha)
    return tuple(sorted(line for line in output.splitlines() if line.strip()))


def build_pr_plan(repo: Path, state: Path, run_id: str, issue_url: str, *,
                  allow_workflow_step: bool = False) -> PRPlan:
    """读取 V7/V8 的最终交付证据并确认 Git HEAD、远程基准及修改范围。"""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("无效 run_id")
    repo = repo.expanduser().resolve(strict=True)
    state = state.expanduser().resolve(strict=True)
    if repo == state or state.is_relative_to(repo):
        raise ValueError("状态目录必须位于 Git 仓库之外")
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("--repo 必须是 Git 仓库根目录")
    owner, name, issue_number = _issue_identity(issue_url)
    _assert_origin(repo, owner, name)
    session = _read_json(state / "sessions" / f"{run_id}.json")
    workflow = _read_json(state / "workflows" / f"{run_id}.json")
    report = _read_json(state / "reports" / f"{run_id}.json")
    if session.get("repo") != str(repo) or session.get("run_id") != run_id:
        raise ValueError("会话与本地 Git 仓库或 run_id 不匹配")
    if not isinstance(session.get("task"), str) or issue_url not in session["task"]:
        raise PermissionError("本次 DevPilot 任务并非来自指定 Issue，拒绝关联错误 PR")
    if workflow.get("id") != run_id:
        raise PermissionError("V7 Workflow 与任务编号不匹配")
    steps = workflow.get("steps", {})
    if allow_workflow_step:
        if workflow.get("status") not in {"running", "waiting_approval"}:
            raise PermissionError("PR 步骤只能由正在执行的 V7 工作流触发")
        required_steps = ("delivery_review", "report", "publish_review")
        if session.get("delivery") != "github-pr" or session.get("issue_url") != issue_url:
            raise PermissionError("V7 会话未授权指定 GitHub Issue 的 PR 发布")
        if (not isinstance(steps.get("publish_pr"), dict) or
                steps["publish_pr"].get("status") != "running"):
            raise PermissionError("调用者必须是当前 V7 publish_pr 动作")
        if (not isinstance(steps.get("publish_review"), dict) or
                (steps["publish_review"].get("approval_request") or {}).get("status") != "approved"):
            raise PermissionError("GitHub PR 发布未经人工审核")
    else:
        if workflow.get("status") != "completed":
            raise PermissionError("V7 Workflow 未完成，禁止创建 PR")
        required_steps = ("preapproval", "merge", "verify", "delivery_review", "report")
    if not all(isinstance(steps.get(step), dict) and steps[step].get("status") == "succeeded"
               for step in required_steps):
        raise PermissionError("V7 必要步骤尚未成功")
    request = steps["delivery_review"].get("approval_request") or {}
    if request.get("status") != "approved":
        raise PermissionError("最终交付尚未获得 V7 审批")
    if report.get("run_id") != run_id or report.get("status") != "ready_for_delivery":
        raise PermissionError("报告不属于当前已审批任务")
    goal = report.get("goal", {})
    if not isinstance(goal, dict) or goal.get("goal_status") != "verified" or goal.get("delivery_status") != "ready_for_delivery":
        raise PermissionError("V8 Goal 未通过验收或证据无效")
    criteria = goal.get("criteria", [])
    if not criteria or any(item.get("required") and item.get("status") != "pass" for item in criteria):
        raise PermissionError("V8 必要验收条件缺少 PASS 证据")
    worker_jobs = report.get("worker_jobs", {})
    source_steps = steps
    if session.get("mode") == "repair_delivery" and allow_workflow_step:
        source_run_id = session.get("source_worker_run_id")
        if not isinstance(source_run_id, str) or not _RUN_ID.fullmatch(source_run_id):
            raise PermissionError("返工交付缺少已执行的 Worker 来源")
        source_run = _read_json(state / "workflows" / f"{source_run_id}.json")
        if source_run.get("status") != "completed":
            raise PermissionError("V8 返工工作流尚未完成")
        source_steps = source_run.get("steps", {})
    if not isinstance(worker_jobs, dict) or not all(
        source_steps.get(item.get("id"), {}).get("status") == "succeeded"
        and isinstance(worker_jobs.get(item.get("id")), str) and worker_jobs[item["id"]]
        for item in session.get("plan", {}).get("workers", [])
    ):
        raise PermissionError("V4/V5 Worker 没有完整的任务及后台 Job 证据")
    base_sha, head_sha = session.get("base_head"), report.get("git_head")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("Git 基准 SHA 无效")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("Git 交付 SHA 无效")
    if report.get("git_base") != base_sha or _git(repo, "rev-parse", "HEAD") != head_sha:
        raise PermissionError("工作区 HEAD 与已验收的 Git 证据不一致")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise PermissionError("工作区不干净，禁止发布")
    _git(repo, "merge-base", "--is-ancestor", base_sha, head_sha)
    changed = _git_changes(repo, base_sha, head_sha)
    if not changed or list(changed) != sorted(report.get("changed_files") or []):
        raise PermissionError("改动文件与交付报告不一致")
    expected = {name for item in session.get("plan", {}).get("workers", []) for name in item.get("files", [])}
    if session.get("mode") == "repair_delivery" and allow_workflow_step:
        parent_id = session.get("parent_run_id")
        if not isinstance(parent_id, str) or not _RUN_ID.fullmatch(parent_id):
            raise PermissionError("返工交付缺少原始任务来源")
        parent = _read_json(state / "sessions" / f"{parent_id}.json")
        if parent.get("goal_id") != session.get("goal_id") or parent.get("issue_url") != issue_url:
            raise PermissionError("返工交付没有与原始 Goal 和 Issue 绑定")
        expected |= {name for item in parent.get("plan", {}).get("workers", []) for name in item.get("files", [])}
    if not set(changed).issubset(expected) or any(not name.endswith(".py") for name in changed):
        raise PermissionError("实际修改超出了审批计划中的 Python 文件")
    remote_main = _remote_sha(repo, "main")
    if remote_main != base_sha:
        raise PermissionError("远程 main 已改变或与任务基准不一致，请人工重新集成")
    branch = f"devpilot/issue-{issue_number}-{run_id[:8]}"
    title = f"fix: resolve GitHub Issue #{issue_number}"
    body = (f"Fixes #{issue_number}\n\n"
            "## DevPilot verification\n"
            f"- Workflow run: `{run_id}`\n"
            "- V7 Workflow: completed; human delivery review: approved\n"
            "- V8 Goal: verified\n"
            f"- Source revision: `{head_sha}`\n"
            f"- Modified files: {', '.join('`' + f + '`' for f in changed)}\n\n"
            "Generated from verified local changes; please review in GitHub before merging.\n")
    return PRPlan(run_id, str(repo), f"{owner}/{name}", issue_url, issue_number,
                  "main", base_sha, head_sha, branch, changed, session.get("test_mode", "pytest"), title, body)


def rerun_tests(plan: PRPlan) -> str:
    """发布前使用当前 Python 环境重新运行宿主限定的真实测试命令。"""
    if plan.test_mode == "pytest":
        argv = [sys.executable, "-m", "pytest", "-q"]
    elif plan.test_mode == "unittest":
        argv = [sys.executable, "-m", "unittest", "discover", "-v"]
    else:
        raise ValueError("不支持的测试模式")
    output = _process(argv, Path(plan.repo), timeout=180)
    if plan.test_mode == "pytest" and not re.search(r"\b\d+ passed\b", output):
        raise RuntimeError("pytest 未报告任何通过的测试")
    if plan.test_mode == "unittest" and not re.search(r"Ran [1-9]\d* tests?", output):
        raise RuntimeError("unittest 未发现有效测试")
    return output[-1000:]


def _list_prs(plan: PRPlan) -> list[dict[str, Any]]:
    """从 GitHub 查询同一修复提交的 PR，防止因重试创建重复 PR。"""
    output = _gh(Path(plan.repo), "pr", "list", "--repo", plan.github_repo, "--state", "all",
                 "--limit", "100", "--json", "url,state,baseRefName,headRefName,headRefOid")
    result = json.loads(output)
    if not isinstance(result, list):
        raise RuntimeError("GitHub PR 列表格式错误")
    return result


def _check_pr_match(plan: PRPlan, existing: dict[str, Any]) -> str:
    """只接受与已验收提交、目标 main 和指定仓库完全一致的 PR。"""
    url = existing.get("url", "")
    prefix = f"https://github.com/{plan.github_repo}/pull/"
    if (not isinstance(url, str) or not url.startswith(prefix) or
            not url[len(prefix):].isdigit() or existing.get("headRefOid") != plan.head_sha or
            existing.get("baseRefName") != "main"):
        raise RuntimeError("发现同名或同提交但不同目标的 PR，需要人工核实")
    if existing.get("state") != "OPEN":
        raise RuntimeError("PR 已关闭或合并，拒绝自动再次发布")
    return url


def _save_receipt(state: Path, plan: PRPlan, url: str) -> Path:
    """在仓库外以原子替换保存发布记录，后续重复调用可以核对历史。"""
    target = state / "github_pr" / f"{plan.run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": "published", "pr_url": url, "run_id": plan.run_id,
               "issue_url": plan.issue_url, "branch": plan.publish_branch,
               "head_sha": plan.head_sha, "confirmed_locally": True,
               "published_at": datetime.now(timezone.utc).isoformat()}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def publish_pr(plan: PRPlan, state: Path, confirm: Callable[[PRPlan], bool]) -> dict[str, Any]:
    """取得真人确认后只推送新的修复分支、创建 PR 并持久化回执，永不合并 main。"""
    repo = Path(plan.repo)
    state = state.resolve(strict=True)
    receipt = state / "github_pr" / f"{plan.run_id}.json"
    if receipt.is_file():
        existing = _read_json(receipt)
        if (existing.get("head_sha") != plan.head_sha or existing.get("branch") != plan.publish_branch or
                existing.get("issue_url") != plan.issue_url or existing.get("status") != "published"):
            raise RuntimeError("已存在冲突的发布回执，需要人工检查")
    if not confirm(plan):
        raise PermissionError("未得到本地显式发布批准，不执行远程写操作")
    rerun_tests(plan)
    if _git(repo, "rev-parse", "HEAD") != plan.head_sha or _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise PermissionError("测试后仓库发生变化，拒绝发布")
    if _remote_sha(repo, plan.base_branch) != plan.base_sha:
        raise PermissionError("发布前远程 main 已变化，拒绝推送")
    prs = _list_prs(plan)
    found = [item for item in prs if item.get("headRefName") == plan.publish_branch or
             (item.get("headRefOid") == plan.head_sha and item.get("baseRefName") == "main")]
    if len(found) > 1:
        raise RuntimeError("同一提交存在多个 PR，需要人工核实")
    if found:
        url = _check_pr_match(plan, found[0])
        path = _save_receipt(state, plan, url)
        return {"passed": True, "reused": True, "pr_url": url, "receipt": str(path), "merged": False}
    remote_sha = _remote_sha(repo, plan.publish_branch)
    if remote_sha is not None and remote_sha != plan.head_sha:
        raise RuntimeError("远程修复分支已存在不同提交，拒绝覆盖")
    if remote_sha is None:
        _git(repo, "push", "origin", f"{plan.head_sha}:refs/heads/{plan.publish_branch}")
    prs = _list_prs(plan)
    found = [item for item in prs if item.get("headRefName") == plan.publish_branch]
    if not found:
        _gh(repo, "pr", "create", "--repo", plan.github_repo, "--base", "main",
            "--head", plan.publish_branch, "--title", plan.title, "--body", plan.body)
        prs = _list_prs(plan)
        found = [item for item in prs if item.get("headRefName") == plan.publish_branch]
    if len(found) != 1:
        raise RuntimeError("无法唯一确认 GitHub PR，停止操作；请检查远端再重试")
    url = _check_pr_match(plan, found[0])
    path = _save_receipt(state, plan, url)
    return {"passed": True, "reused": False, "pr_url": url,
            "branch": plan.publish_branch, "receipt": str(path), "merged": False}
