"""V9.4：通过离线自检或真实 DeepSeek 加本地 MCP stdio 协议验收完整修复链。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from uuid import uuid4

from devpilot.core.message import ToolCall
from devpilot.core.model import DeepSeekModel, ModelResponse
from devpilot.mcp.results import MCPToolInfo, MCPToolResult
from devpilot.runtime.full_chain import V94Integration


def git(repo: Path, *args: str) -> str:
    """只让可信宿主在临时测试仓库内运行固定 Git 操作。"""
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, timeout=20, check=True)
    return result.stdout.strip()


def create_fixture(root: Path) -> Path:
    """创建带有真实登录 Bug 的隔离 Git 仓库，供 Worker 修改和 V8 验证。"""
    root.mkdir(parents=True, exist_ok=False)
    (root / "auth.py").write_text(
        'def login(password: str) -> int:\n'
        '    if password == "correct":\n'
        '        return 200\n'
        '    return 500\n', encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (root / "README.md").write_text("# Issue #15\n错误密码应返回 401，正确密码应返回 200。\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "DevPilot Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial fixture")
    return root


class OfflineIssueBridge:
    """仅在无 MCP SDK 的离线自测中模拟服务端响应，不得据此宣称真实 MCP 连接成功。"""

    def start(self) -> None:
        """将离线协议替身标记为可以接受固定夹具调用。"""
        self.started = True

    def list_tools(self) -> list[MCPToolInfo]:
        """返回与真实 Server 相同的 get_issue 输入结构，供 V6 原适配器发现工具。"""
        return [MCPToolInfo("get_issue", "Read fixture issue", {
            "type": "object", "properties": {"issue_number": {"type": "integer"}},
            "required": ["issue_number"], "additionalProperties": False,
        })]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """通过 V6 调用路径返回固定 Issue 文本，但不启动真正的 MCP Server。"""
        if name != "get_issue" or arguments != {"issue_number": 15}:
            return MCPToolResult(name, True, ("Unknown fixture issue",), None, ())
        return MCPToolResult(name, False, ("Issue #15: 修复登录错误密码状态码500为401；保留200并补充unittest。",), None, ())

    def close(self) -> None:
        """释放离线替身的生命周期标记。"""
        self.started = False


class TracedModel:
    """统一限制每个 Agent 最多调用模型的次数，并记录真实返回的工具名称。"""

    def __init__(self, inner: Any, agent_id: str, *, max_calls: int = 16):
        """包装脚本或 DeepSeek 模型，拒绝无界推理循环。"""
        self.inner = inner
        self.agent_id = agent_id
        self.max_calls = max_calls
        self.calls = 0
        self.requested: list[str] = []

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> ModelResponse:
        """转发真实模型调用并保存返回的工具请求，超过安全上限立即停止。"""
        if self.calls >= self.max_calls:
            raise RuntimeError(f"Model call budget exhausted for {self.agent_id}")
        self.calls += 1
        reply = self.inner.complete(messages=messages, tools=tools, system=system)
        self.requested.extend(call.name for call in reply.tool_calls)
        return reply


class OfflineCodingModel:
    """只用于验证原有 Agent Loop 和运行时接线的可重复脚本模型。"""

    def __init__(self, agent_id: str, *, make_wrong_fix: bool = False):
        """根据 Agent 身份生成一次合法工具调用并保存已完成的阶段。"""
        self.agent_id = agent_id
        self.used: set[str] = set()
        self.make_wrong_fix = make_wrong_fix
        self.calls = 0

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> ModelResponse:
        """让真实 V0 Agent Loop 调用 V1 工具创建任务、Worker 和真实代码文件。"""
        self.calls += 1
        latest_user = next((str(msg.content) for msg in reversed(messages) if msg.role == "user"), "")
        if self.agent_id != "lead":
            if "write" not in self.used:
                self.used.add("write")
                if self.agent_id == "backend":
                    status = 500 if self.make_wrong_fix else 401
                    return ModelResponse("修复实际登录逻辑", [ToolCall(uuid4().hex, "write_file", {
                        "path": "auth.py", "content": f'def login(password: str) -> int:\n    return 200 if password == "correct" else {status}\n',
                    })])
                return ModelResponse("添加真实回归测试", [ToolCall(uuid4().hex, "write_file", {
                    "path": "test_auth.py", "content": (
                        "import unittest\nfrom auth import login\n\n"
                        "class LoginTests(unittest.TestCase):\n"
                        "    def test_bad_password(self):\n        self.assertEqual(login(\"wrong\"), 401)\n"
                        "    def test_good_password(self):\n        self.assertEqual(login(\"correct\"), 200)\n"
                    ),
                })])
            return ModelResponse("Worker 已提交本地代码写入请求。")
        if "STAGE:PLAN" in latest_user and "plan" not in self.used:
            self.used.add("plan")
            return ModelResponse("实际通过 MCP 复核 Issue 并创建任务", [
                ToolCall(uuid4().hex, "mcp__fixture__get_issue", {"issue_number": 15}),
                ToolCall(uuid4().hex, "task_create", {"title": "后端登录修复", "description": "只修改 auth.py，保持正确密码200，错误密码401"}),
                ToolCall(uuid4().hex, "task_create", {"title": "登录回归测试", "description": "只新增 test_auth.py，编写 unittest 测试实际 auth.login"}),
            ])
        if "STAGE:SPAWN" in latest_user and "spawn" not in self.used:
            self.used.add("spawn")
            return ModelResponse("创建两位队友", [
                ToolCall(uuid4().hex, "team_spawn", {"agent_id": "backend", "role": "登录修复"}),
                ToolCall(uuid4().hex, "team_spawn", {"agent_id": "tests", "role": "回归测试"}),
            ])
        if "STAGE:DISPATCH" in latest_user and "dispatch" not in self.used:
            self.used.add("dispatch")
            return ModelResponse("将任务提交给真实后台线程", [
                ToolCall(uuid4().hex, "team_submit_ready", {"max_assignments": 2}),
            ])
        return ModelResponse("已经读取后台完成通知。")


def run_acceptance(root: Path, *, live: bool, authorize_fixture_approval: bool = False,
                   make_wrong_fix: bool = False, mcp_authorizer: Any | None = None) -> dict[str, Any]:
    """使用真实 Git、V0～V8 运行完整流程，离线模式单独标记而不冒充在线通过。"""
    root.mkdir(parents=True, exist_ok=False)
    repo = create_fixture(root / "fixture_repo")
    state = root / "state"
    models: dict[str, Any] = {}

    def model_factory(agent_id: str) -> Any:
        """在线使用真正 DeepSeekModel，离线则保留每位 Agent 的脚本调用记录。"""
        inner = DeepSeekModel(model=os.environ.get("DEVPILOT_MODEL", "deepseek-v4-flash")) if live else OfflineCodingModel(agent_id, make_wrong_fix=make_wrong_fix)
        if live and hasattr(inner, "client"):
            inner.client = inner.client.with_options(timeout=60.0, max_retries=1)
        wrapped = TracedModel(inner, agent_id)
        models[agent_id] = wrapped
        return wrapped

    bridge_factory = None if live else lambda _definition: OfflineIssueBridge()
    with V94Integration(repo, state, model_factory, mcp_bridge_factory=bridge_factory, mcp_authorizer=mcp_authorizer) as app:
        workflow_id, goal_id = app.begin()
        status = app.advance(workflow_id, timeout_seconds=180 if live else 45)
        approval_requested = len(app.workflow.pending_approvals(workflow_id)) == 1
        if status == "waiting_approval" and authorize_fixture_approval:
            request = app.workflow.pending_approvals(workflow_id)[0]
            status = app.approve(workflow_id, actor="reviewer", request_id=request.request_id, approved=True)
        result = app.result(workflow_id, goal_id)
        workflow = app.workflow.store.get(workflow_id)
        criteria = {name: record.status for name, record in app.goal.get(goal_id).criteria.items()}
        jobs = [j for j in app.team.jobs() if j.name.startswith("team:")]
        lead_msgs = app.team.core.lead.messages
        mcp_calls = [call.id for msg in lead_msgs if msg.role == "assistant" for call in msg.tool_calls
                     if call.name == "mcp__fixture__get_issue"]
        mcp_answers = [msg.content for msg in lead_msgs if msg.role == "tool" and msg.tool_call_id in mcp_calls]
        checks = {
            "lead_model_requested_mcp": len(mcp_calls) >= 1,
            "lead_mcp_tool_result_success": any(x.startswith("[MCP RESULT: ") for x in mcp_answers),
            "mcp_tool_succeeded": workflow.steps["issue"].status == "succeeded" and bool(workflow.steps["issue"].output),
            "two_real_tasks": len(app.team.tasks()) == 2,
            "two_real_workers": sorted(app.team.teammates()) == ["backend", "tests"],
            "two_completed_v5_jobs": len(jobs) == 2 and all(j.status == "completed" for j in jobs),
            "two_delivered_events": len(set(result.delivered_events)) == 2,
            "git_merge_committed": workflow.steps["merge"].status == "succeeded" and bool(result.git_head),
            "goal_criteria_all_pass": bool(criteria) and all(s == "pass" for s in criteria.values()),
            "goal_verified": result.goal_status == "verified",
            "approval_requested": approval_requested,
            "manual_review_respected": (result.approved and result.workflow_status == "completed") if authorize_fixture_approval else (
                not result.approved and result.workflow_status == "waiting_approval"),
        }
        delivery = None
        if result.approved and result.workflow_status == "completed":
            delivery = app.delivery_report(workflow_id, goal_id)
        checks["delivery_follows_approval"] = (delivery is not None and delivery["delivery_status"] == "ready_for_delivery") if authorize_fixture_approval else delivery is None
        checks["git_worktree_clean"] = app.goal.verifier._git_state()[1] is False
        checks["real_model_calls"] = all(getattr(obj, "calls", 0) > 0 for obj in models.values()) and len(models) == 3
        # 实际工具调用、真实 SDK 会话必须由在线模式另行核对，离线替身不等于 SDK 连接成功。
        outcome = {
            "model_mode": "real_deepseek_and_real_mcp_stdio" if live else "offline_scripted_with_mcp_bridge_stub",
            "passed": live and all(checks.values()),
            "offline_self_test_passed": not live and all(checks.values()),
            "checks": checks,
            "workflow_id": workflow_id, "goal_id": goal_id,
            "workflow_status": result.workflow_status, "goal_status": result.goal_status,
            "review_approved": result.approved, "git_head": result.git_head,
            "criteria": criteria, "worker_job_ids": list(result.worker_job_ids),
            "delivered_event_ids": list(result.delivered_events),
            "model_call_count": {name: obj.calls for name, obj in models.items()},
            "model_requested_tools": {name: obj.requested for name, obj in models.items()},
            "delivery": delivery,
        }
        return outcome


def write_report(root: Path, result: dict[str, Any]) -> None:
    """生成不含 API Key 或完整 Prompt 的 JSON 和 Markdown 验收结果。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# V9.4 实际 MCP + Workflow + Goal 验收", "",
             f"模型模式：`{result['model_mode']}`", "",
             f"在线通过：`{result['passed']}`；离线自检：`{result['offline_self_test_passed']}`", "",
             "| 检查项 | 结果 |", "|---|---|"]
    lines.extend(f"| {key} | {'PASS' if value else 'FAIL'} |" for key, value in result["checks"].items())
    lines.extend(["", f"Workflow：`{result.get('workflow_status', 'unknown')}`", f"Goal：`{result.get('goal_status', 'unknown')}`", "",
                  "**离线自测不等于真实 DeepSeek 或 MCP SDK 已通过；正式在线模式必须包含真实服务端连接。**", ""])
    (root / "result.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """提供分离的离线自检和真实 DeepSeek/MCP 验收命令，并明确要求测试仓库审批授权。"""
    parser = argparse.ArgumentParser(description="V9.4 full chain acceptance")
    parser.add_argument("--live", action="store_true", help="real DeepSeek + real local stdio MCP Server")
    parser.add_argument("--authorize-fixture-approval", action="store_true", help="approve only the isolated fixture after review")
    parser.add_argument("--output", type=Path, help="new output path")
    args = parser.parse_args()
    folder = args.output or (Path(".devpilot/v9_4_acceptance") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]))
    folder = folder.resolve()
    try:
        result = run_acceptance(folder, live=args.live, authorize_fixture_approval=args.authorize_fixture_approval)
    except Exception as exc:
        secret = os.getenv("DEEPSEEK_API_KEY", "")
        reason = str(exc).replace(secret, "[REDACTED]") if secret else str(exc)
        reason = re.sub(r"(?i)sk-[a-z0-9_-]{12,}", "[REDACTED]", reason)
        result = {"model_mode": "real_deepseek_and_real_mcp_stdio" if args.live else "offline_scripted_with_mcp_bridge_stub",
                  "passed": False, "offline_self_test_passed": False, "checks": {"completed": False},
                  "failure": f"{type(exc).__name__}: {reason[:240]}"}
    write_report(folder, result)
    print("V9.4 report:", folder / "result.md")
    print("Result:", "PASS" if result["passed"] else "OFFLINE SELF TEST" if result["offline_self_test_passed"] else "FAIL")
    if (args.live and not result["passed"]) or (not args.live and not result["offline_self_test_passed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
