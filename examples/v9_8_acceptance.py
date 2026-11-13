"""V9.8：将离线完整回归和真实 DeepSeek+HTTP MCP+GitHub PR 验收严格分开。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def offline_suite() -> dict[str, Any]:
    """独立子进程逐组运行旧版与新增真实组件测试，避免后台线程影响其他测试组。"""
    files = ("tests/test_v9_8_unified_integration.py", "tests/test_v9_8_components_on_demand.py",
             "tests/test_v9_6_general_coder.py", "tests/test_v9_7_auto_pr.py",
             "tests/test_v9_7_github_issue_coding.py")
    groups = []
    with tempfile.TemporaryDirectory(prefix="devpilot-v98-tests-") as directory:
        for i, filename in enumerate(files):
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "--basetemp=" + str(Path(directory) / f"group_{i}"), filename]
            try:
                result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                        check=False, timeout=180)
                groups.append({"file": filename, "passed": result.returncode == 0,
                               "returncode": result.returncode,
                               "output_tail": (result.stdout + "\n" + result.stderr)[-1500:]})
            except subprocess.TimeoutExpired:
                groups.append({"file": filename, "passed": False, "timeout": True})
    return {"mode": "OFFLINE_COMPONENT_REGRESSION", "offline_passed": all(row["passed"] for row in groups),
            "online_passed": False, "groups": groups}



def verify_live(repo: Path, state: Path, run_id: str, issue_url: str, trace_file: Path) -> dict[str, Any]:
    """通过真实 MCP 读取回执、DeepSeek 轨迹、V7/V8 状态和 GitHub PR 元数据核对在线交付。"""
    from devpilot.runtime.model_trace import TraceWriter
    if not (len(run_id) == 32 and all(ch in "0123456789abcdef" for ch in run_id)):
        raise ValueError("Invalid V7 run_id")
    session = json.loads((state / "sessions" / f"{run_id}.json").read_text(encoding="utf-8"))
    workflow = json.loads((state / "workflows" / f"{run_id}.json").read_text(encoding="utf-8"))
    report = json.loads((state / "reports" / f"{run_id}.json").read_text(encoding="utf-8"))
    receipt_file = state / "mcp_issue_receipts" / (hashlib.sha256(issue_url.encode("utf-8")).hexdigest() + ".json")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8")) if receipt_file.is_file() else {}
    trace = TraceWriter(trace_file).summarize()
    trace_fresh = False
    try:
        receipt_time = datetime.fromisoformat(receipt.get("read_at_utc", ""))
        if trace_file.is_file():
            for line in trace_file.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if (item.get("kind") == "api_response" and
                        datetime.fromisoformat(item.get("timestamp_utc", "")) >= receipt_time):
                    trace_fresh = True
                    break
    except (TypeError, ValueError, KeyError, OSError):
        trace_fresh = False
    pr = report.get("github_pr") or {}
    checks = {
        "issue_mcp_read": receipt.get("source_issue") == issue_url and receipt.get("mcp_call_executed") is True
                          and receipt.get("origin_matches_issue") is True and bool(receipt.get("mcp_result_sha256")),
        "issue_bound_to_run": session.get("issue_url") == issue_url and session.get("repo") == str(repo),
        "deepseek_api_responses": trace_fresh and trace.get("api_responses", 0) >= 1
                                  and trace.get("api_errors", 0) == 0,
        "v7_workflow_completed": workflow.get("status") == "completed"
                                 and all(step.get("status") == "succeeded" for step in workflow.get("steps", {}).values()),
        "v8_goal_verified": (report.get("goal") or {}).get("goal_status") == "verified"
                            and (report.get("goal") or {}).get("delivery_status") == "ready_for_delivery",
        "pr_url_saved": isinstance(pr.get("url"), str) and pr["url"].startswith("https://github.com/")
                        and pr.get("merged") is False,
    }
    if checks["pr_url_saved"]:
        try:
            call = subprocess.run(["gh", "pr", "view", pr["url"], "--json", "url,state,baseRefName,headRefOid"],
                                  cwd=repo, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            if call.returncode == 0:
                remote = json.loads(call.stdout)
                checks["github_pr_exists"] = (remote.get("url") == pr["url"]
                    and remote.get("baseRefName") == "main"
                    and remote.get("headRefOid") == report.get("git_head")
                    and remote.get("state") in {"OPEN", "MERGED"})
            else:
                checks["github_pr_exists"] = False
        except (OSError, subprocess.TimeoutExpired, ValueError):
            checks["github_pr_exists"] = False
    else:
        checks["github_pr_exists"] = False
    return {"mode": "LIVE_DEEPSEEK_GITHUB_ISSUE_PR", "run_id": run_id,
            "online_passed": all(checks.values()), "checks": checks,
            "trace_file": str(trace_file), "pr_url": pr.get("url"),
            "note": "必须提供同一实际任务的独立 MCP 回执、真实模型轨迹、GitHub PR 元数据。"}


def main(argv: list[str] | None = None) -> int:
    """提供 offline、live 和 verify 三个明确的真实验收入口，不自动跳过审批。"""
    parser = argparse.ArgumentParser(description="DevPilot V9.8 unified workflow acceptance")
    parser.add_argument("--mode", choices=("offline", "live", "verify"), default="offline")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--issue-url")
    parser.add_argument("--config", type=Path, default=Path("examples/github_mcp_http_readonly.json"))
    parser.add_argument("--trace-file", type=Path)
    parser.add_argument("--allow-file", action="append", default=[])
    args = parser.parse_args(argv)
    if args.mode == "offline":
        result = offline_suite()
    else:
        if not args.repo or not args.issue_url or not args.trace_file:
            parser.error("live/verify require --repo --issue-url --trace-file")
        repo = args.repo.resolve(strict=True)
        trace = args.trace_file.expanduser().resolve()
        if trace.is_relative_to(repo):
            parser.error("trace file must be outside the repository")
        if args.mode == "live":
            from examples.v9_7_github_issue_coding import run_bridge
            arguments = ["--mode", "run", "--delivery", "github-pr", "--issue-url", args.issue_url,
                         "--repo", str(repo), "--config", str(args.config), "--trace-file", str(trace)]
            for filename in args.allow_file or ["shop/pricing.py"]:
                arguments.extend(["--allow-file", filename])
            return run_bridge(arguments)
        if not args.run_id:
            parser.error("verify requires --run-id")
        from devpilot.cli import default_state
        state = (args.state or default_state(repo)).resolve()
        result = verify_live(repo, state, args.run_id, args.issue_url, trace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("online_passed") or result.get("offline_passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
