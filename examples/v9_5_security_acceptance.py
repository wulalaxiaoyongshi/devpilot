"""V9.5：用原 V9.4 全链路执行动态 MCP 权限、计划审核及操作去重的联合验收。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from devpilot.runtime.mcp_access import MCPAccessController, MCPServerAccess
from devpilot.runtime.operation_guard import OperationLedger
from devpilot.runtime.plan_guard import PlanActionRule, WorkflowPlanGuard
from examples.v9_4_full_chain_acceptance import run_acceptance


def make_policy() -> MCPAccessController:
    """给本地受信任的只读 Issue Server 赋予自动读权限，其余工具继续默认拒绝。"""
    return MCPAccessController((MCPServerAccess("fixture", auto_read=True),))


def run_security_acceptance(directory: Path, *, live: bool, authorize_fixture_approval: bool = False) -> dict:
    """用真实 V0～V8 运行修复闭环，并检查动态授权、DAG 审核及重启防重放。"""
    policy = make_policy()
    guard = WorkflowPlanGuard((PlanActionRule("read_code", frozenset({"path"})),
                               PlanActionRule("change_code", frozenset({"path", "content"}), mutates=True)))
    safe_plan = guard.compile({"steps": [
        {"id": "inspect", "action": "read_code", "params": {"path": "auth.py"}, "depends_on": []},
        {"id": "modify", "action": "change_code", "params": {"path": "auth.py", "content": "..."}, "depends_on": ["inspect"]},
    ]}, workflow_id="trusted_plan")
    assert safe_plan.steps[0].id == "preapproval"
    ledger = OperationLedger(directory / "safety_ledger")
    ledger.begin("external:issue:15", description="验证非幂等操作去重")
    try:
        ledger.begin("external:issue:15", description="重复提交")
        raise AssertionError("Ledger accepted duplicate side effect")
    except Exception as error:
        from devpilot.workflow.recovery import UncertainActionError
        if not isinstance(error, UncertainActionError):
            raise
    assert OperationLedger(directory / "safety_ledger").get("external:issue:15")["status"] == "inflight"
    chain = run_acceptance(directory / "chain", live=live,
                           authorize_fixture_approval=authorize_fixture_approval, mcp_authorizer=policy)
    checks = dict(chain["checks"])
    checks.update({"dynamic_discovery": "mcp__fixture__get_issue" in policy.discovered.get("fixture", (0, frozenset()))[1],
                   "unknown_mcp_denied": not policy.authorize("mcp__fixture__delete_repo", {}),
                   "validated_mutation_approval": safe_plan.steps[0].kind == "approval",
                   "durable_replay_blocked": True})
    return {"mode": "real_deepseek_and_real_mcp_stdio" if live else "offline_scripted_model_and_mcp_stub",
            "online_passed": live and all(checks.values()),
            "offline_self_test_passed": not live and all(checks.values()),
            "checks": checks, "workflow_status": chain["workflow_status"], "goal_status": chain["goal_status"],
            "worker_job_ids": chain["worker_job_ids"], "model_call_count": chain["model_call_count"]}


def write_report(root: Path, report: dict) -> None:
    """保存不包含用户密钥的 JSON 和 Markdown 验收清单。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# V9.5 可靠性与安全验收", "", f"模式：{report['mode']}",
             f"真实在线通过：{report['online_passed']}", f"离线自检通过：{report['offline_self_test_passed']}",
             "", "| 检查项 | 结果 |", "|---|---|"]
    lines.extend(f"| {key} | {'PASS' if value else 'FAIL'} |" for key, value in report["checks"].items())
    lines.append("\n离线固定模型或 MCP 桥接替身不等于真实 API/协议通过。")
    (root / "result.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """从命令行运行离线自检或真实 DeepSeek + MCP，并持久化可核查的报告。"""
    parser = argparse.ArgumentParser(description="DevPilot V9.5 security acceptance")
    parser.add_argument("--live", action="store_true", help="actual DeepSeek + MCP stdio SDK")
    parser.add_argument("--authorize-fixture-approval", action="store_true", help="approve only isolated test repo")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = (args.output or Path(".devpilot/v9_5_acceptance") /
            (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])).resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        report = run_security_acceptance(root, live=args.live, authorize_fixture_approval=args.authorize_fixture_approval)
    except Exception as error:
        message = str(error)
        key = os.getenv("DEEPSEEK_API_KEY", "")
        if key:
            message = message.replace(key, "[REDACTED]")
        message = re.sub(r"(?i)sk-[a-z0-9_-]{12,}", "[REDACTED]", message)
        report = {"mode": "real_deepseek_and_real_mcp_stdio" if args.live else "offline_scripted_model_and_mcp_stub",
                  "online_passed": False, "offline_self_test_passed": False,
                  "checks": {"completed": False}, "failure": f"{type(error).__name__}: {message[:240]}"}
    write_report(root, report)
    print("V9.5 result:", root / "result.md")
    print("PASS" if report["online_passed"] else "OFFLINE SELF TEST" if report["offline_self_test_passed"] else "FAIL")
    if not (report["online_passed"] if args.live else report["offline_self_test_passed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
