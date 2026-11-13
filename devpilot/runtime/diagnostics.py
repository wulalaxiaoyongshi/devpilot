"""V9.7：在真正调用模型前检查 Coding Agent 的本机依赖、Git 与授权边界。"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _module_available(name: str, symbol: str | None = None) -> bool:
    """只验证 Python SDK 的实际导入能力，不发送任何网络请求。"""
    try:
        module = importlib.import_module(name)
        return symbol is None or callable(getattr(module, symbol, None))
    except (ImportError, AttributeError, RuntimeError):
        return False


def _has_deepseek_key() -> bool:
    """只检查进程环境或当前工作目录 .env 中是否配置密钥，绝不输出其内容。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return True
    try:
        from dotenv import dotenv_values, find_dotenv
        source = find_dotenv(usecwd=True)
        return bool(source and dotenv_values(source).get("DEEPSEEK_API_KEY"))
    except (ImportError, OSError, ValueError):
        return False


def environment_report(repo: Path, state: Path, *, require_live: bool = False,
                       mcp_config: Path | None = None, test_mode: str = "pytest") -> dict[str, Any]:
    """只读取仓库、运行时和配置状态，区分离线可运行与真实模型在线就绪。"""
    root, storage = Path(repo).resolve(), Path(state).resolve()
    checks: dict[str, bool] = {
        "python_3_12_or_newer": sys.version_info >= (3, 12),
        "repo_is_directory": root.is_dir(),
        "state_outside_repo": not (storage == root or storage.is_relative_to(root)),
        "git_executable": shutil.which("git") is not None,
        "test_tool_supported": test_mode in {"pytest", "unittest"},
        "test_runner_available": test_mode == "unittest" or _module_available("pytest"),
        "openai_sdk_available": _module_available("openai", "OpenAI"),
        "deepseek_api_key_configured": _has_deepseek_key(),
    }
    if checks["repo_is_directory"] and checks["git_executable"]:
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                 capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False)
            checks["repo_is_git_root"] = top.returncode == 0 and Path(top.stdout.strip()).resolve() == root
            changes = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                                     capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False)
            checks["git_worktree_clean"] = changes.returncode == 0 and not changes.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            checks["repo_is_git_root"] = checks["git_worktree_clean"] = False
    else:
        checks["repo_is_git_root"] = checks["git_worktree_clean"] = False
    if mcp_config is not None:
        checks["mcp_sdk_available"] = _module_available("mcp.server", "MCPServer")
        try:
            from devpilot.mcp.server_config import load_server_definitions
            definitions = load_server_definitions(mcp_config)
            checks["mcp_config_loadable"] = bool(definitions)
        except (ImportError, OSError, ValueError, TypeError, KeyError):
            checks["mcp_config_loadable"] = False
    basic_names = {"python_3_12_or_newer", "repo_is_directory", "state_outside_repo",
                   "git_executable", "test_tool_supported", "test_runner_available",
                   "repo_is_git_root", "git_worktree_clean"}
    ready_offline = all(checks[name] for name in basic_names)
    ready_live = ready_offline and checks["openai_sdk_available"] and checks["deepseek_api_key_configured"]
    if mcp_config is not None:
        ready_live = ready_live and checks["mcp_sdk_available"] and checks["mcp_config_loadable"]
    return {"ready": bool(ready_live if require_live else ready_offline),
            "offline_ready": ready_offline, "live_ready": bool(ready_live),
            "checks": checks, "repo": str(root), "state": str(storage),
            "note": "仅静态检查依赖与配置，live_ready 不代表 DeepSeek 或 MCP 网络调用成功"}
