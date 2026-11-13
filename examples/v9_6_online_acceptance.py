"""V9.6：在隔离 Git 项目上验证通用 CLI 的完整运行链与真实 DeepSeek 模型。"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from uuid import uuid4

from devpilot.core.message import ToolCall
from devpilot.core.model import DeepSeekModel, ModelResponse
from devpilot.runtime.general_coder import GeneralCodingAgent, git


class OfflineModel:
    """用固定的模型回包检查原运行时，不把离线结果冒充 DeepSeek 成功。"""

    def __init__(self, role: str):
        """保存当前模型身份与工具调用轮次。"""
        self.role = role
        self.count = 0

    def complete(self, messages, tools=None, system=None):
        """模拟分析、计划和修复加法函数的模型选择，但调用原项目真实工具。"""
        self.count += 1
        if self.role == "planner":
            return ModelResponse(json.dumps({"summary": "修复 add 的减法 Bug", "workers": [
                {"id": "repair", "task": "修改 mathbox.py 的 add(a,b)，让它返回 a + b", "files": ["mathbox.py"]}]}))
        if self.role == "lead":
            if self.count == 1:
                return ModelResponse("", [ToolCall("inspect_1", "read_file", {"path": "mathbox.py"})])
            return ModelResponse("Existing add uses subtraction instead of addition")
        if self.count == 1:
            return ModelResponse("", [ToolCall("edit_1", "write_file", {"path": "mathbox.py", "content": "def add(a, b):\n    return a + b\n"})])
        return ModelResponse("Updated add")


class TracedModel:
    """只记录模型真实请求次数和生成的 ToolCall 名称，不收集 API Key。"""

    def __init__(self, inner, role: str, trace: dict[str, dict]):
        """包装真实或离线模型以便生成可复核的端到端验收证据。"""
        self.inner, self.role, self.trace = inner, role, trace
        self.trace.setdefault(role, {"calls": 0, "tool_calls": [], "tool_results": [], "tool_request_ids": {}})

    def complete(self, messages, tools=None, system=None):
        """执行一次实际模型请求并记录模型实际要求调用的工具名称。"""
        for item in messages:
            if item.role == "tool" and item.tool_call_id not in {seen["id"] for seen in self.trace[self.role]["tool_results"]}:
                content = item.content or ""
                self.trace[self.role]["tool_results"].append({"id": item.tool_call_id, "tool": self.trace[self.role]["tool_request_ids"].get(item.tool_call_id),
                    "mcp_confirmed": "[MCP RESULT:" in content, "ok": not (
                    content.startswith("Error:") or content.startswith("Permission denied") or content.startswith("Tool error:")),
                    "read_confirmed": "def add" in content, "write_confirmed": "Wrote" in content})
        response = self.inner.complete(messages, tools=tools, system=system)
        self.trace[self.role]["calls"] += 1
        self.trace[self.role]["tool_calls"].extend(call.name for call in response.tool_calls)
        self.trace[self.role]["tool_request_ids"].update({call.id: call.name for call in response.tool_calls})
        return response


def create_fixture(root: Path) -> None:
    """建立真实且独立的 Git 项目，保留可由 pytest 证明的加法 Bug。"""
    root.mkdir(parents=True)
    (root / "mathbox.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_mathbox.py").write_text("from mathbox import add\n\ndef test_add():\n    assert add(2,3) == 5\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    for command in (("init",), ("config", "user.name", "DevPilot Acceptance"),
                    ("config", "user.email", "devpilot-acceptance@example.invalid"),
                    ("add", "-A"), ("commit", "-m", "broken fixture")):
        subprocess.run(["git", *command], cwd=root, capture_output=True, text=True, check=True)


def run_acceptance(directory: Path, *, live: bool, authorize_fixture_approval: bool = False, model_name: str = "deepseek-v4-flash",
                   mcp_config: Path | None = None, mcp_read_aliases: tuple[str, ...] = ()) -> dict:
    """以真实 Git 和 V0～V8 组件执行目标修复，仅在显式授权时批准隔离仓库。"""
    directory.mkdir(parents=True, exist_ok=True)
    repo = directory / "fixture_repo"
    create_fixture(repo)
    trace: dict[str, dict] = {}
    factory = lambda role: TracedModel(DeepSeekModel(model=model_name) if live else OfflineModel(role), role, trace)
    checks = {}
    status = None
    error = None
    with GeneralCodingAgent(repo, directory / "state", factory, test_mode="pytest",
                            mcp_config=mcp_config, mcp_read_aliases=mcp_read_aliases) as app:
        try:
            status = app.create("修复 mathbox.py 中 add 的错误减法实现，要求 2+3=5，保留 test_mathbox.py 原测试内容。")
            run_id = status["run_id"]
            checks["plan_approval_required"] = status["workflow"] == "waiting_approval"
            if authorize_fixture_approval and checks["plan_approval_required"]:
                first = status["approvals"][0]
                plan = app.session(run_id)["plan"]
                checks["test_file_not_editable"] = all("test_mathbox.py" not in item["files"] for item in plan["workers"])
                if checks["test_file_not_editable"]:
                    app.approve(run_id, first["step"], first["request_id"], actor="reviewer", approved=True)
                    status = app.advance(run_id, timeout_seconds=180)
                    checks["goal_verified"] = status["goal"] == "verified"
                    checks["delivery_approval_required"] = status["workflow"] == "waiting_approval"
                    if all(checks.values()):
                        second = status["approvals"][0]
                        app.approve(run_id, second["step"], second["request_id"], actor="reviewer", approved=True)
                        status = app.advance(run_id, timeout_seconds=30)
                        checks["workflow_completed"] = status["workflow"] == "completed"
                        checks["real_code_fixed"] = (repo / "mathbox.py").read_text(encoding="utf-8").find("return a + b") != -1
                        checks["commit_changed_file"] = "mathbox.py" in git(repo, "diff", "--name-only", app.session(run_id)["base_head"], "HEAD").splitlines()
                        checks["report_created"] = (directory / "state" / "reports" / f"{run_id}.json").is_file()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
    tools = [name for item in trace.values() for name in item["tool_calls"]]
    checks["lead_read_file_tool_call"] = "read_file" in trace.get("lead", {}).get("tool_calls", [])
    checks["worker_write_file_tool_call"] = "write_file" in tools
    checks["lead_read_file_executed"] = any(r["ok"] and r["read_confirmed"] for r in trace.get("lead", {}).get("tool_results", []))
    checks["worker_write_file_executed"] = any(r["ok"] and r["write_confirmed"] for role, record in trace.items() if role not in {"lead", "planner"} for r in record["tool_results"])
    checks["model_calls_observed"] = any(item["calls"] for item in trace.values())
    if mcp_config is not None:
        checks["mcp_read_tool_executed"] = any(
            item["mcp_confirmed"] and item["ok"] and (item["tool"] or "").startswith("mcp__")
            for record in trace.values() for item in record["tool_results"]
        )
    report = {"mode": "LIVE_DEEPSEEK" if live else "OFFLINE_SCRIPT_MODEL", "online_passed": live and bool(checks) and all(checks.values()) and error is None,
              "offline_passed": not live and authorize_fixture_approval and bool(checks) and all(checks.values()) and error is None,
              "checks": checks, "trace": trace, "workflow_status": status["workflow"] if status else None,
              "goal_status": status["goal"] if status else None, "error": error}
    (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "result.md").write_text("# V9.6 端到端验收\n\n" + f"模式：{report['mode']}\n\n在线通过：{report['online_passed']}\n\n" +
        "\n".join(f"- {name}: {'PASS' if passed else 'FAIL'}" for name, passed in checks.items()) +
        f"\n\n错误：{error or '无'}\n", encoding="utf-8")
    return report


def main() -> int:
    """提供真实 DeepSeek 模式和明确标注的离线模式，并保存完整验收结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实调用 DeepSeek API")
    parser.add_argument("--authorize-fixture-approval", action="store_true", help="仅授权本脚本建立的隔离测试仓库")
    parser.add_argument("--output", type=Path, default=Path(".devpilot/v9_6_acceptance") / uuid4().hex)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--mcp-config", type=Path)
    parser.add_argument("--mcp-read-alias", action="append", default=[])
    args = parser.parse_args()
    report = run_acceptance(args.output, live=args.live, authorize_fixture_approval=args.authorize_fixture_approval,
                            model_name=args.model, mcp_config=args.mcp_config, mcp_read_aliases=tuple(args.mcp_read_alias))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"验收目录：{args.output.resolve()}")
    return 0 if report["online_passed"] or report["offline_passed"] or (not args.authorize_fixture_approval and not report["error"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
