"""检查 GitHub Issue 桥接入口的本地安全边界，不接触网络或 API 密钥。"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


_SOURCE = Path(__file__).resolve().parents[1] / "examples" / "v9_7_github_issue_coding.py"
_SPEC = importlib.util.spec_from_file_location("devpilot_github_issue_coding_e2e", _SOURCE)
assert _SPEC and _SPEC.loader
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


def _git(root: Path, *args: str) -> str:
    """为本地测试创建独立 Git 仓库并返回命令输出。"""
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip()


def test_parse_issue_url():
    """解析标准 HTTPS GitHub Issue URL。"""
    assert bridge.parse_issue_url("https://github.com/hello-user/lab-repo/issues/12") == ("hello-user", "lab-repo", 12)


@pytest.mark.parametrize("url", [
    "https://evil.example/owner/repo/issues/1",
    "http://github.com/owner/repo/issues/1",
    "https://github.com/owner/repo/pull/1",
    "https://github.com/owner/repo/issues/1?foo=bar",
    "https://github.com/owner/repo/issues/0",
])
def test_reject_invalid_issue_urls(url):
    """拒绝非 GitHub 域名、非 HTTPS 和非 Issue 网址。"""
    with pytest.raises(ValueError):
        bridge.parse_issue_url(url)


def test_only_allow_exact_issue_read():
    """只允许读取同仓库同编号 Issue，不放行其他 GitHub 工具。"""
    args = {"owner": "a", "repo": "b", "issue_number": 4, "method": "get"}
    assert bridge.exact_issue_read_allowed("mcp__github__issue_read", args, "a", "b", 4, "github")
    assert not bridge.exact_issue_read_allowed("mcp__github__issue_write", args, "a", "b", 4, "github")
    assert not bridge.exact_issue_read_allowed("mcp__github__issue_read", {**args, "issue_number": 5}, "a", "b", 4, "github")


def test_task_has_user_constraints():
    """给真实模型的任务保留外部资料边界和不推送远端的限制。"""
    task = bridge.build_issue_task("https://github.com/a/b/issues/1", "README 样例问题")
    assert "<issue_untrusted_content>" in task and "shop/pricing.py" in task and "不得 push" in task


def test_reject_overlong_issue():
    """拒绝过长 Issue 避免无界外部文本进入模型上下文。"""
    with pytest.raises(ValueError):
        bridge.build_issue_task("https://github.com/a/b/issues/1", "x" * 9501)


def test_git_repo_checks_origin_and_clean(tmp_path):
    """检查本地 origin 对应 GitHub Issue 且仓库没有未提交改动。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "remote", "add", "origin", "https://github.com/example/lab.git")
    assert bridge.ensure_matching_git_repo(root, "example", "lab") == root
    with pytest.raises(ValueError, match="不匹配"):
        bridge.ensure_matching_git_repo(root, "another", "lab")
    (root / "dirty.txt").write_text("untracked", encoding="utf-8")
    with pytest.raises(ValueError, match="干净"):
        bridge.ensure_matching_git_repo(root, "example", "lab")
