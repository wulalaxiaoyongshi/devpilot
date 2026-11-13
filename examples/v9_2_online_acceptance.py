"""V9.2：使用真实模型完成仓库分析、工具调用、记忆查询和任务落盘验收。"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
from uuid import uuid4

from devpilot.runtime.lead_analysis import create_lead_analysis_app
from devpilot.tasks.store import TaskStore


FORBIDDEN_TOOLS = ("bash", "write_file", "edit_file", "task_update", "team_spawn", "team_submit_ready")


def create_fixture(root: Path) -> Path:
    """在独立目录中建立故意包含登录 Bug 的无敏感信息 Git 仓库。"""
    root.mkdir(parents=True, exist_ok=False)
    (root / "auth.py").write_text(
        'def login(password: str) -> int:\n'
        '    if password == "correct":\n'
        '        return 200\n'
        '    return 500  # BUG: 错误密码应返回 401\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# 登录演示项目\n错误密码应返回 401，正确密码返回 200。\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def safe_error(error: Exception) -> str:
    """把错误说明中的常见 API 密钥格式遮盖后再写入验收报告。"""
    message = str(error)
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        message = message.replace(key, "[REDACTED]")
    message = re.sub(r"(?i)(sk-[a-z0-9_-]{12,})", "[REDACTED]", message)
    return f"{type(error).__name__}: {message[:300]}"


class ModelProbe:
    """包裹模型客户端，统计真实 complete 调用和模型发出的工具请求，不记录原始 Prompt。"""

    def __init__(self, model: Any, marker: str, max_calls: int = 15):
        """记录底层模型和用于检查上下文注入的非敏感标记。"""
        self.model = model
        self.marker = marker
        self.max_calls = max_calls
        self.calls = 0
        self.model_tool_requests: list[str] = []
        self.context_present = False
        self.memory_present = False
        self.tool_result_observed = False

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> Any:
        """调用真实模型前后只采集元数据，并保持底层 Model 接口不变。"""
        if self.calls >= self.max_calls:
            raise RuntimeError(f"V9.2 在线验收已达到模型请求上限 {self.max_calls}")
        self.calls += 1
        prompt = system or ""
        self.context_present |= "## Current Todo State" in prompt and "## Available Skills" in prompt
        self.memory_present |= self.marker in prompt and "## Relevant Persistent Memory" in prompt
        self.tool_result_observed |= any(getattr(m, "role", None) == "tool" for m in messages)
        result = self.model.complete(messages=messages, tools=tools, system=system)
        self.model_tool_requests.extend(call.name for call in result.tool_calls)
        return result


class ToolProbe:
    """记录真正经过 V1 Dispatcher 的执行状态，而不是只看模型声称用了什么工具。"""

    def __init__(self, dispatcher: Any, marker: str, target_file: str):
        """保存真正的分发方法和非敏感验收标记，以便检查工具返回内容。"""
        self.marker = marker
        self.target_file = Path(target_file).as_posix()
        self.dispatcher = dispatcher
        self.original_execute = dispatcher.execute
        self.original_detailed = dispatcher.execute_detailed
        self.events: list[dict[str, Any]] = []

    def install(self) -> None:
        """在本轮验收会话中替换分发入口并保留原始 Handler 执行路径。"""
        self.dispatcher.execute = self.execute

    def execute(self, call: Any) -> str:
        """通过真实权限 Hook 和 Handler 执行工具，并仅保存名称、状态及文本错误标志。"""
        result = self.original_detailed(call)
        self.events.append({
            "name": call.name,
            "status": result.status,
            "returned_error_text": result.content.startswith("Error:") or result.content.startswith("Permission denied"),
            "target_listed": call.name == "glob" and self.target_file in result.content,
            "target_read": call.name == "read_file" and Path(str(call.arguments.get("path", ""))).as_posix() == self.target_file and bool(result.content.strip()),
            "memory_seed_found": call.name == "memory_search" and self.marker in result.content,
        })
        return result.content


def inspect_result(model_probe: ModelProbe, tool_probe: ToolProbe, task_ids: set[str],
                   persisted_ids: set[str], before_sha: str, after_sha: str, allowed_names: tuple[str, ...],
                   online: bool, task_relevant: bool, tasks_pending: bool) -> dict[str, bool]:
    """将在线证据转换成固定的通过/失败门槛，避免仅凭语言回答宣布成功。"""
    executed = Counter(e["name"] for e in tool_probe.events if e["status"] == "returned" and not e["returned_error_text"])
    successful = [e for e in tool_probe.events if e["status"] == "returned" and not e["returned_error_text"]]
    checks = {
        "model_complete_invoked": model_probe.calls > 0,
        "agent_loop_tool_feedback": model_probe.tool_result_observed,
        "v1_glob_executed": executed["glob"] > 0,
        "v1_glob_found_target": any(e["target_listed"] for e in successful),
        "v1_read_file_executed": executed["read_file"] > 0,
        "v1_read_file_returned_target": any(e["target_read"] for e in successful),
        "v2_context_injected": model_probe.context_present,
        "v3_memory_injected": model_probe.memory_present,
        "v3_memory_search_executed": executed["memory_search"] > 0,
        "v3_memory_search_recalled_seed": any(e["memory_seed_found"] for e in successful),
        "v3_task_create_executed": executed["task_create"] > 0,
        "v3_task_persisted": bool(task_ids) and task_ids.issubset(persisted_ids),
        "v3_task_matches_goal": task_relevant,
        "v3_tasks_pending": tasks_pending,
        "analysis_source_unchanged": before_sha == after_sha,
        "mutating_tools_not_registered": all(name not in allowed_names for name in FORBIDDEN_TOOLS),
        "no_forbidden_tool_executed": not any(e["name"] in FORBIDDEN_TOOLS for e in tool_probe.events),
        "no_tool_denied_or_exception": all(e["status"] == "returned" and not e["returned_error_text"] for e in tool_probe.events),
    }
    return checks


def run_acceptance(repo: Path, state_dir: Path, model_factory: Callable[[str], Any],
                   *, online: bool, model_name: str, target_file: str = "auth.py",
                   max_steps: int = 8, max_calls: int = 15, include_analysis: bool = False) -> dict[str, Any]:
    """逐轮驱动真实 Lead 验证读取、记忆和任务落盘，并返回不含源码及密钥的结构化结果。"""
    source = (repo / target_file).resolve()
    if not source.is_file() or not source.is_relative_to(repo.resolve()):
        raise ValueError("target_file must be an existing file inside the repository")
    initial_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    marker = "V92MEMORY_" + uuid4().hex[:12]
    probes: list[ModelProbe] = []

    def traced_model(agent_id: str) -> ModelProbe:
        """由真正的工厂注入模型探针，只保留实际 Lead 的模型调用统计。"""
        model = model_factory(agent_id)
        if online and hasattr(model, "client"):
            model.client = model.client.with_options(timeout=50.0, max_retries=1)
        probe = ModelProbe(model, marker, max_calls=max_calls)
        if agent_id == "lead":
            probes.append(probe)
        return probe

    state_dir.mkdir(parents=True, exist_ok=True)
    tool_probe = None
    model_probe = None
    new_task_ids: set[str] = set()
    available_tools: tuple[str, ...] = ()
    phase_results = []
    with create_lead_analysis_app(repo, traced_model, state_dir=state_dir) as app:
        model_probe = probes[0]
        lead = app.core.lead
        lead.max_steps = max_steps
        tool_probe = ToolProbe(lead.dispatcher, marker, target_file)
        tool_probe.install()
        available_tools = app.tools()
        initial_ids = {task.id for task in app.tasks()}
        lead.memory.remember(f"{target_file} login 期望错误密码返回 401；验收标记 {marker}", "project_fact", tags=["auth.py", "login", "401"])
        phases = [
            ("inspect", f"这是 V9.2 在线验收，不得修改任何文件。请务必先调用 glob 查找 Python 文件，再调用 read_file 读取 {target_file}，用实际源码指出登录错误码问题。不要创建任务。"),
            ("recall", f"请使用 memory_search 工具查询 '{target_file} login 401' 的持久记忆，说明该记忆和仓库源码有什么联系。不要只根据系统提示直接回答。"),
            ("plan", f"请调用 task_create，在持久 Task Graph 中创建一条 pending 任务，标题与修复 {target_file} 的错误密码状态码有关，描述包括要求返回 401、需要添加回归测试。必须真的调用工具，不能只用自然语言列计划。"),
        ]
        for phase_name, request in phases:
            before = len(tool_probe.events)
            text = app.analyze(request)
            phase_results.append({
                "phase": phase_name,
                "completed": bool(text.strip()),
                **({"reply_preview": text[:1200]} if include_analysis else {}),
                "tool_names": [event["name"] for event in tool_probe.events[before:]],
            })
        new_task_ids = {task.id for task in app.tasks()} - initial_ids

    reopened = TaskStore(state_dir / "tasks.json")
    saved_tasks = reopened.all()
    persisted_ids = {task.id for task in saved_tasks}
    new_tasks = [task for task in saved_tasks if task.id in new_task_ids]
    task_relevant = any("401" in (task.title + " " + task.description)
                        and any(term in (task.title + " " + task.description).lower()
                                for term in ("登录", "login", "密码", "auth")) for task in new_tasks)
    tasks_pending = bool(new_tasks) and all(task.status == "pending" for task in new_tasks)
    after_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = inspect_result(model_probe, tool_probe, new_task_ids, persisted_ids,
                            initial_sha, after_sha, available_tools, online, task_relevant, tasks_pending)
    return {
        "live_evidence": bool(online),
        "model_mode": "real_deepseek_api" if online else "offline_scripted_self_test",
        "model_name": model_name,
        "model_complete_calls": model_probe.calls,
        "model_requested_tools": model_probe.model_tool_requests,
        "v1_dispatcher_events": tool_probe.events,
        "new_task_count": len(new_task_ids),
        "persisted_task_count": len(persisted_ids),
        "source_sha256_unchanged": initial_sha == after_sha,
        "phase_results": phase_results,
        "checks": checks,
        "passed": all(checks.values()) and online,
        "self_test_passed": all(checks.values()) and not online,
    }


class ScriptedSelfTestModel:
    """只供离线检测验收脚本本身是否正常工作，绝不冒充 DeepSeek 在线运行。"""

    def __init__(self):
        """记录固定脚本模型的轮数，便于完整遍历 V1～V3。"""
        self.round = 0

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> Any:
        """按固定顺序发出工具请求，验证验收仪表本身。"""
        from devpilot.core.message import ToolCall
        from devpilot.core.model import ModelResponse
        self.round += 1
        steps = {
            1: ToolCall("self-1", "glob", {"pattern": "**/*.py"}),
            2: ToolCall("self-2", "read_file", {"path": "auth.py"}),
            4: ToolCall("self-4", "memory_search", {"query": "auth.py login 401"}),
            6: ToolCall("self-6", "task_create", {"title": "修复错误密码返回 500", "description": "返回 401 并增加回归测试"}),
        }
        if self.round in steps:
            return ModelResponse("调用真实工具", [steps[self.round]])
        return ModelResponse("本轮操作已结束")


def write_report(folder: Path, report: dict[str, Any]) -> None:
    """保存不含原始 Prompt、源码内容和 API Key 的 JSON 与简明 Markdown 验收结果。"""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    checks = report.get("checks", {})
    lines = ["# DevPilot V9.2 验收报告", "", f"执行模式：{report['mode']}",
             f"{'在线验收' if report['mode'] == 'live' else '离线自检'}结果：{'PASS' if (report.get('passed') if report['mode'] == 'live' else report.get('self_test_passed')) else 'FAIL'}", "",
             f"模型请求次数：{report.get('model_complete_calls', 0)}", "",
             "## 检查项", ""]
    lines.extend(f"- {'PASS' if value else 'FAIL'} — `{key}`" for key, value in checks.items())
    for phase in report.get("phase_results", []):
        if "reply_preview" in phase:
            lines.extend(["", f"## {phase['phase']} 模型回复（样例仓库）", "", phase["reply_preview"]])
    if report.get("error"):
        lines.extend(["", f"执行错误：{report['error']}"])
    (folder / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """只在显式 --live 且配置本地密钥时请求 DeepSeek，并以退出码表示验收结果。"""
    parser = argparse.ArgumentParser(description="DevPilot V9.2 真实模型端到端验收")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="真实 DeepSeek API，产生实际 API 费用")
    mode.add_argument("--offline-self-test", action="store_true", help="仅验证测试脚本，不代表模型在线验收")
    parser.add_argument("--repo", type=Path, default=None, help="自有 Git 仓库；省略则使用安全的临时样例仓库")
    parser.add_argument("--allow-user-repo", action="store_true", help="明确允许发送指定仓库的受限代码到模型")
    parser.add_argument("--target-file", default="auth.py", help="仓库内的相对源文件路径")
    parser.add_argument("--output", type=Path, default=Path(".devpilot/v9_2_online_acceptance"))
    parser.add_argument("--model", default=None, help="覆盖本地 DeepSeekModel 的模型名")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-calls", type=int, default=15, help="单次验收最多发起多少次模型 complete 请求")
    args = parser.parse_args(argv)
    if args.repo is not None and not args.allow_user_repo:
        parser.error("分析已有仓库必须显式添加 --allow-user-repo，确认源码可以发送给外部模型")
    if args.max_calls < 3 or args.max_calls > 60:
        parser.error("--max-calls 必须介于 3 和 60")
    if args.max_steps < 3 or args.max_steps > 30:
        parser.error("--max-steps 必须介于 3 和 30")
    if args.live:
        from dotenv import load_dotenv
        load_dotenv()
        if not os.environ.get("DEEPSEEK_API_KEY"):
            parser.error("缺少 DEEPSEEK_API_KEY，请在你自己的 .env 中配置，不要把密钥发给别人")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    folder = args.output.resolve() / run_id
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"run_id": run_id, "mode": "live" if args.live else "offline-self-test", "passed": False}
    try:
        if args.repo is None:
            repo = create_fixture(folder / "fixture_repo")
            target = "auth.py"
        else:
            repo = args.repo.resolve()
            target = args.target_file
            subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                           check=True, capture_output=True)
        if args.live:
            from devpilot.core.model import DeepSeekModel
            factory = lambda _id: DeepSeekModel(model=args.model) if args.model else DeepSeekModel()
            name = args.model or "DeepSeekModel 默认模型"
        else:
            factory = lambda _id: ScriptedSelfTestModel()
            name = "SCRIPTED — NOT LIVE"
        report.update(run_acceptance(repo, folder / "state", factory, online=args.live,
                                     model_name=name, target_file=target, max_steps=args.max_steps,
                                     max_calls=args.max_calls,
                                     include_analysis=args.repo is None))
    except Exception as error:
        report["error"] = safe_error(error)
        print("执行错误：", report["error"], file=sys.stderr)
    finally:
        write_report(folder, report)
    outcome = report.get("passed") if args.live else report.get("self_test_passed")
    print(f"{'在线验收' if args.live else '离线自检'}结果：{'PASS' if outcome else 'FAIL'}")
    print(f"报告路径：{folder / 'result.md'}")
    if not args.live:
        print("注意：离线自检不能用于证明 DeepSeek 真实调用。")
    return 0 if outcome else 1


if __name__ == "__main__":
    raise SystemExit(main())
