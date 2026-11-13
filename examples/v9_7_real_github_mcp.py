"""连接 GitHub 官方 MCP Server，核对发现、V1 授权、真实调用和模型选择工具的轨迹。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from devpilot.core.message import ToolCall
from devpilot.mcp.multi_server import MCPMultiServerManager
from devpilot.mcp.permission import MCPPermissionPolicy
from devpilot.mcp.server_config import load_server_definitions
from devpilot.runtime.mcp_access import MCPAccessController, MCPServerAccess
from devpilot.runtime.model_trace import TraceWriter, TracedDeepSeekModel
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.permissions import PermissionPolicy
from devpilot.tools.registry import ToolRegistry

DEFAULT_ARGUMENTS = {"owner": "github", "repo": "github-mcp-server", "path": "README.md"}


def safe_result(content: str) -> dict[str, Any]:
    """返回真实工具结果的成功标记、长度与哈希，不将 GitHub 原文写进日志。"""
    return {
        "success": content.startswith("[MCP RESULT:"),
        "business_status": content.split("\n", 1)[0][:180],
        "output_chars": len(content),
        "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def run_direct(config: Path, alias: str, tool: str, arguments: dict[str, Any], execute: bool) -> dict[str, Any]:
    """复用 V6 多 Server、V9.5 授权和 V1 Dispatcher 进行真实工具发现及可选调用。"""
    registry = ToolRegistry()
    defs = load_server_definitions(config)
    if alias not in [item.alias for item in defs]:
        raise ValueError("指定的 alias 不在配置文件中")
    manager = MCPMultiServerManager(registry, defs)
    try:
        manager.connect_all(strict=True)
        access = MCPAccessController((MCPServerAccess(alias, auto_read=True),))
        access.sync_discovery(manager)
        status = manager.status(alias)
        report: dict[str, Any] = {
            "server": alias, "server_state": status.state.value,
            "generation": status.generation, "discovered_tools": list(status.tools),
            "tool_requested": tool, "call_attempted": execute,
        }
        if execute:
            hooks = HookManager()
            hooks.register_pre_tool_use(MCPPermissionPolicy(PermissionPolicy(confirmer=lambda _prompt: False),
                                                             approve=access.authorize).check)
            execution = ToolDispatcher(registry, hooks).execute_detailed(ToolCall("mcp-live-test", tool, arguments))
            report["v1_dispatch_status"] = execution.status
            report.update(safe_result(execution.content))
            report["passed"] = execution.status == "returned" and report["success"]
        else:
            report["passed"] = status.state.value == "connected" and bool(status.tools)
        return report
    finally:
        manager.close()


def run_agent(config: Path, alias: str, tool: str, arguments: dict[str, Any], repo: Path,
              model_name: str, trace: TraceWriter) -> dict[str, Any]:
    """让真实 DeepSeek 通过原 Agent Loop 自主选择 MCP 工具，并核对实际工具执行结果。"""
    from devpilot.runtime.factory import create_app

    repo = repo.resolve(strict=True)
    if not repo.is_dir():
        raise ValueError("--repo 必须指向现有本地 Git 项目")
    if trace.path == repo or trace.path.is_relative_to(repo):
        raise ValueError("trace-file 不能位于 Git 仓库内")
    access = MCPAccessController((MCPServerAccess(alias, auto_read=True),))
    with TemporaryDirectory(prefix="devpilot_mcp_live_") as temp:
        core = create_app(
            repo, lambda role: TracedDeepSeekModel(model_name, role=role, trace=trace),
            approve=lambda name, args: name.startswith("mcp__") and access.authorize(name, args),
            state_dir=Path(temp), lead_mode="analysis", auto_extract=False,
        )
        manager = MCPMultiServerManager(core.lead.tools, load_server_definitions(config))
        executed: list[dict[str, Any]] = []
        try:
            manager.connect_all(strict=True)
            access.sync_discovery(manager)
            status = manager.status(alias)
            if tool not in status.tools:
                raise ValueError(f"目标工具未被 Server 发现：{tool}")
            if not access.authorize(tool, arguments):
                raise PermissionError(f"目标工具未通过 V9.5 只读授权：{tool}")

            def record_tool(call: ToolCall, result: str) -> None:
                """仅在原 Agent 的 V1 Dispatcher 实际调用 Handler 后记录 MCP 返回状态。"""
                if call.name.startswith(f"mcp__{alias}__"):
                    details = safe_result(result)
                    trace.append("mcp_tool_executed", name=call.name, **details)
                    executed.append({"name": call.name, **details})

            core.lead.hooks.register_post_tool_use(record_tool)
            trace.append("mcp_agent_start", alias=alias, tool=tool, discovered_tools=len(status.tools))
            answer = core.chat(
                "这次专门测试外部 MCP 真实工具调用。请调用 " + tool +
                "，参数为 " + json.dumps(arguments, ensure_ascii=False) +
                "，先读取远端真实内容，再用两句话总结，并明确说出你调用的 MCP 工具名称。"
                "不要根据先验知识编造结果，也不要调用写入或 Shell 工具。"
            )
            summary = trace.summarize()
            reported = tool in summary["tool_calls_proposed"]
            actual = any(x["name"] == tool and x["success"] for x in executed)
            return {
                "mode": "LIVE_DEEPSEEK_EXTERNAL_GITHUB_MCP",
                "server": alias, "server_state": status.state.value,
                "tool": tool, "model_tool_call_proposed": reported,
                "mcp_actual_success": actual,
                "model_api_responses": summary["api_responses"],
                "answer_excerpt": answer[:800],
                "passed": bool(reported and actual and summary["api_responses"]),
                "trace_file": str(trace.path),
            }
        finally:
            manager.close()
            core.shutdown()


def main(argv: list[str] | None = None) -> int:
    """用 list、call、agent 三种模式逐层核对外部 MCP 的真实发现和执行。"""
    parser = argparse.ArgumentParser(description="DevPilot 真实 GitHub MCP 只读联调")
    parser.add_argument("--mode", choices=("list", "call", "agent"), default="list")
    parser.add_argument("--config", type=Path, default=Path("examples/github_mcp_readonly.json"))
    parser.add_argument("--alias", default="github")
    parser.add_argument("--tool", default="mcp__github__get_file_contents")
    parser.add_argument("--arguments", default=json.dumps(DEFAULT_ARGUMENTS))
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--model", default=os.environ.get("DEVPILOT_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--trace-file", type=Path, default=Path.home() / "devpilot_github_mcp_trace.jsonl")
    parser.add_argument("--output", type=Path, default=Path.home() / "devpilot_github_mcp_result.json")
    args = parser.parse_args(argv)
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        parser.error("--arguments 必须是 JSON 对象")
    try:
        if args.mode == "agent":
            if args.repo is None:
                parser.error("--mode agent 必须传 --repo")
            result = run_agent(args.config, args.alias, args.tool, arguments, args.repo,
                               args.model, TraceWriter(args.trace_file))
        else:
            result = run_direct(args.config, args.alias, args.tool, arguments, args.mode == "call")
    except (ValueError, RuntimeError, OSError, PermissionError, TimeoutError) as error:
        result = {"passed": False, "mode": args.mode, "error_type": type(error).__name__,
                  "error": str(error)[:350]}
    args.output = args.output.expanduser().resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
