"""V9.3：真实 DeepSeek 多 Agent、Git Worktree、后台 Job、事件通知的可复现验收入口。"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable
from uuid import uuid4

from devpilot.core.message import ToolCall
from devpilot.core.model import DeepSeekModel, ModelResponse
from devpilot.runtime.lead_team import create_lead_team_app
from devpilot.tasks.store import TaskStore
from devpilot.background.store import JobStore
from devpilot.runtime.notifications import LeadNoticeStore


ALLOWED_FILES = ("backend_fix.py", "test_auth.py")


def git(repo: Path, *args: str) -> str:
    """仅由验收宿主在临时仓库运行固定 Git 子命令，不向模型暴露 Shell。"""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def create_fixture(root: Path) -> Path:
    """创建独立的真实 Git 仓库，提供无需网络或私人文件的登录 Bug。"""
    root.mkdir(parents=True, exist_ok=False)
    (root / "auth.py").write_text(
        'def login(password: str) -> int:\n'
        '    if password == "correct":\n'
        '        return 200\n'
        '    return 500  # BUG: 应返回 401\n', encoding="utf-8")
    (root / "README.md").write_text("# Login fixture\n错误密码必须返回401，正确密码返回200。\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "DevPilot Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", "auth.py", "README.md")
    git(root, "commit", "-q", "-m", "fixture")
    return root


class TracedModel:
    """统计每个 Lead/Worker 的真实模型调用和工具请求，不保存原始提示词或 API Key。"""

    def __init__(self, inner: Any, agent_id: str, max_calls: int = 16):
        """绑定真实或离线模型，限制 API 请求数量并保存代理名称。"""
        self.inner = inner
        self.agent_id = agent_id
        self.max_calls = max_calls
        self.calls = 0
        self.requested: list[str] = []

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> Any:
        """记录实际请求并转发给底层模型，超出请求预算时立即停止。"""
        if self.calls >= self.max_calls:
            raise RuntimeError(f"Model call limit reached for {self.agent_id}")
        self.calls += 1
        response = self.inner.complete(messages=messages, tools=tools, system=system)
        self.requested.extend(call.name for call in response.tool_calls)
        return response


class ToolTrace:
    """记录真实 V1 Dispatcher 的工具执行状态，而非依赖模型在文本里宣称完成。"""

    def __init__(self, agent: Any):
        """保存 Agent 的真实工具分发入口和不包含用户文本的事件清单。"""
        self.agent = agent
        self.original = agent.dispatcher.execute_detailed
        self.events: list[dict[str, Any]] = []

    def install(self) -> None:
        """将 Agent 的 execute 接口接到真实结构化分发器上并收集状态元数据。"""
        self.agent.dispatcher.execute = self.execute

    def execute(self, call: Any) -> str:
        """调用原始 Handler 并记录工具名称、执行状态和最小业务结果信息。"""
        response = self.original(call)
        raw_path = call.arguments.get("path") if isinstance(call.arguments, dict) else None
        safe_path = raw_path if raw_path in (*ALLOWED_FILES, "auth.py") else None
        self.events.append({
            "tool": call.name, "status": response.status,
            "error_text": response.content.startswith(("Error:", "Permission denied", "Approval required")),
            "path": safe_path,
        })
        return response.content


class OfflineTeamModel:
    """仅用于测试集成与验收脚本的固定模型，报告中不会标为真实在线通过。"""

    def __init__(self, agent_id: str):
        """记录是 Lead 还是哪个 Worker，以便产生可重复的工具调用。"""
        self.agent_id = agent_id
        self.used: set[str] = set()

    def complete(self, messages: Any, tools: Any = None, system: str | None = None) -> ModelResponse:
        """在真实 Agent Loop 中按指定顺序创建任务、队友、后台 Job 和受限文件。"""
        latest_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
        if self.agent_id != "lead":
            if "worker-write" not in self.used:
                self.used.add("worker-write")
                filename = "backend_fix.py" if self.agent_id == "backend" else "test_auth.py"
                content = ("def correct_error_code():\n    return 401\n" if filename == "backend_fix.py"
                           else "def test_login_wrong_password():\n    assert 401 == 401\n")
                return ModelResponse("创建任务要求的独立文件", [ToolCall(f"write-{self.agent_id}", "write_file", {"path": filename, "content": content})])
            return ModelResponse("已完成受限代码文件修改")
        if "STAGE:PLAN" in latest_user and "plan" not in self.used:
            self.used.add("plan")
            return ModelResponse("创建两条独立开发任务", [
                ToolCall("task-b", "task_create", {"title": "后端登录修复", "description": "只新增 backend_fix.py；实现错误密码返回 401 的函数。"}),
                ToolCall("task-t", "task_create", {"title": "登录回归测试", "description": "只新增 test_auth.py；编写错误密码应返回 401 的回归测试。"}),
            ])
        if "STAGE:SPAWN" in latest_user and "spawn" not in self.used:
            self.used.add("spawn")
            return ModelResponse("创建两位真实队友", [
                ToolCall("spawn-b", "team_spawn", {"agent_id": "backend", "role": "修复登录逻辑"}),
                ToolCall("spawn-t", "team_spawn", {"agent_id": "tests", "role": "编写登录测试"}),
            ])
        if "STAGE:DISPATCH" in latest_user and "dispatch" not in self.used:
            self.used.add("dispatch")
            return ModelResponse("提交两个真实后台任务", [ToolCall("dispatch", "team_submit_ready", {"max_assignments": 2})])
        return ModelResponse("已读取真实任务和后台事件状态。")


def _phase_prompts() -> list[tuple[str, str]]:
    """为在线模型定义独立的计划、创建队友和后台提交阶段，便于明确定位失败阶段。"""
    return [
        ("plan", "STAGE:PLAN。这是独立临时仓库的 V9.3 在线验收。必须实际调用 task_create 两次，"
         "创建两条无依赖的 pending 任务：第一条只新增 backend_fix.py，实现返回 401 的小函数；"
         "第二条只新增 test_auth.py，编写 401 回归测试。不要写其他文件，也不要直接修改主仓库。"),
        ("spawn", "STAGE:SPAWN。必须实际调用 team_spawn 两次，agent_id 分别为 backend 和 tests，"
         "角色分别为登录修复、回归测试。不可仅在文本里说已创建队友。"),
        ("dispatch", "STAGE:DISPATCH。必须实际调用 team_submit_ready，max_assignments=2，"
         "并向用户解释返回的是 task_id 到 job_id 的真实映射。不要调用 bash。"),
    ]


def run_acceptance(root: Path, model_factory: Callable[[str], Any], *, live: bool,
                   max_model_calls: int = 16, job_timeout: float = 100) -> dict[str, Any]:
    """通过原有 V0～V5 真正执行多 Agent 任务，并在重启读取 JSON 后生成可核查的验收结论。"""
    root.mkdir(parents=True, exist_ok=True)
    repo = create_fixture(root / "fixture_repo")
    state = root / "state"
    models: dict[str, TracedModel] = {}
    traces: dict[str, ToolTrace] = {}
    stages: list[dict[str, Any]] = []
    delivered: list[str] = []
    source_unchanged = (repo / "auth.py").read_text(encoding="utf-8")

    def factory(agent_id: str) -> TracedModel:
        """将原始真实模型包装为轻量探针，保留每个 Worker 独立的模型实例。"""
        inner = model_factory(agent_id)
        if live and not isinstance(inner, DeepSeekModel):
            raise TypeError("Live acceptance requires the real DeepSeekModel, not a script model")
        if live and hasattr(inner, "client"):
            inner.client = inner.client.with_options(timeout=50.0, max_retries=1)
        wrapper = TracedModel(inner, agent_id, max_calls=max_model_calls)
        models[agent_id] = wrapper
        return wrapper

    with create_lead_team_app(repo, factory, state_dir=state, worker_write_files=ALLOWED_FILES,
                              allow_team_dispatch=True, max_workers=2) as app:
        lead_trace = ToolTrace(app.core.lead)
        lead_trace.install()
        traces["lead"] = lead_trace
        app.core.lead.max_steps = 10
        for name, prompt in _phase_prompts():
            before = len(lead_trace.events)
            app.instruct(prompt)
            stages.append({"stage": name, "tools": [event["tool"] for event in lead_trace.events[before:]]})
            if name == "spawn":
                for agent_id, teammate in app.teammates().items():
                    teammate.agent.max_steps = 10
                    trace = ToolTrace(teammate.agent)
                    trace.install()
                    traces[agent_id] = trace
        all_jobs = [job for job in app.jobs() if job.name.startswith("team:")]
        for job in all_jobs:
            app.core.jobs.wait(job.id, timeout=job_timeout)
        result = app.pump(resume_lead=True)
        delivered.extend(notice.event_id for notice in result.delivered_notices)
        repeat = app.pump(resume_lead=True)
        delivered.extend(notice.event_id for notice in repeat.delivered_notices)

    persisted_tasks = TaskStore(state / "tasks.json").all()
    persisted_jobs = JobStore(state / "jobs.json").all()
    persisted_notices = LeadNoticeStore(state / "lead_notices.json").all()
    workers = tuple(sorted(name for name in models if name != "lead"))
    worker_commits = {}
    for agent_id in workers:
        worktree = repo.parent / (repo.name + "-worktrees") / agent_id
        if worktree.exists():
            worker_commits[agent_id] = {"branch": git(worktree, "branch", "--show-current"),
                                        "head": git(worktree, "rev-parse", "HEAD"),
                                        "files": git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()}
    tool_events = {name: trace.events for name, trace in traces.items()}
    succeeded_events = lambda name: [x for x in tool_events.get(name, []) if x["status"] == "returned" and not x["error_text"]]
    lead_ok = Counter(x["tool"] for x in succeeded_events("lead"))
    writes = [x for name in workers for x in succeeded_events(name) if x["tool"] == "write_file"]
    job_rows = [j for j in persisted_jobs if j.name.startswith("team:")]
    task_rows = [t for t in persisted_tasks if t.title in {"后端登录修复", "登录回归测试"}] if not live else persisted_tasks
    checks = {
        "model_complete_invoked": len(models) == 3 and all(m.calls > 0 for m in models.values()),
        "lead_task_create_via_v1": lead_ok["task_create"] >= 2,
        "task_graph_persisted": len(task_rows) == 2 and len(persisted_tasks) == 2 and all(t.status == "completed" for t in task_rows),
        "team_spawn_via_v1": lead_ok["team_spawn"] == 2 and workers == ("backend", "tests"),
        "background_submit_via_v1": lead_ok["team_submit_ready"] >= 1,
        "v5_jobs_persisted": len(job_rows) == 2 and all(j.status == "completed" for j in job_rows),
        "worker_write_tools_executed": len(writes) == 2 and {x["path"] for x in writes} == set(ALLOWED_FILES),
        "worktree_git_commits": set(worker_commits) == {"backend", "tests"} and all(entry["files"] for entry in worker_commits.values()),
        "event_notices_persisted": len(persisted_notices) == 2 and all(n.task_id for n in persisted_notices),
        "lead_pump_consumed_events": len(delivered) == 2 and len(delivered) == len(set(delivered)),
        "main_repo_unchanged": (repo / "auth.py").read_text(encoding="utf-8") == source_unchanged
                               and not (repo / "backend_fix.py").exists() and not (repo / "test_auth.py").exists(),
        "no_unsafe_tools": all(event["tool"] not in {"bash", "edit_file", "cron_create", "job_cancel"}
                                for events in tool_events.values() for event in events),
        "no_denied_or_exception": all(event["status"] == "returned" and not event["error_text"]
                                      for events in tool_events.values() for event in events),
    }
    return {
        "model_mode": "real_deepseek_api" if live else "offline_scripted_self_test",
        "live_evidence": live,
        "model_calls": {name: model.calls for name, model in models.items()},
        "model_requested_tools": {name: model.requested for name, model in models.items()},
        "v1_tool_events": tool_events,
        "stages": stages,
        "task_count": len(persisted_tasks),
        "job_statuses": {j.id: j.status for j in job_rows},
        "event_ids": [notice.event_id for notice in persisted_notices],
        "lead_delivered_event_ids": delivered,
        "worker_commits": worker_commits,
        "checks": checks,
        "passed": live and all(checks.values()),
        "offline_self_test_passed": not live and all(checks.values()),
    }


def write_report(folder: Path, result: dict[str, Any]) -> None:
    """同时保存不含源码和密钥的 JSON、Markdown 测试证据，明确标示离线与在线状态。"""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    outcome = "PASS" if result["passed"] else "OFFLINE_SELF_TEST" if result["offline_self_test_passed"] else "FAIL"
    lines = [f"# V9.3 实际团队集成验收：{outcome}", "", f"模型模式：`{result['model_mode']}`", "",
             "| 验收条件 | 结果 |", "|---|---|",
             *[f"| {name} | {'PASS' if passed else 'FAIL'} |" for name, passed in result["checks"].items()], "",
             "**离线自测不等于真实 DeepSeek 在线通过。**", ""]
    (folder / "result.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """在 Windows 或 Linux 上提供显式 live 授权、离线自检和固定输出目录。"""
    parser = argparse.ArgumentParser(description="V9.3 real multi-agent and event acceptance")
    parser.add_argument("--live", action="store_true", help="connect to real DeepSeek API")
    parser.add_argument("--authorize-isolated-writes", action="store_true", help="explicitly permit writes to two fixture worktrees")
    parser.add_argument("--output", type=Path, help="output folder, default .devpilot/v9_3_online_acceptance/<run_id>")
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()
    if args.live and not args.authorize_isolated_writes:
        parser.error("live mode requires --authorize-isolated-writes")
    folder = args.output or Path(".devpilot/v9_3_online_acceptance") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    if folder.exists():
        parser.error("output folder already exists; use a new empty path")
    folder = folder.resolve()
    factory = (lambda name: DeepSeekModel(model=args.model)) if args.live else (lambda name: OfflineTeamModel(name))
    try:
        result = run_acceptance(folder, factory, live=args.live)
    except Exception as error:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        message = str(error).replace(key, "[REDACTED]") if key else str(error)
        message = re.sub(r"(?i)sk-[a-z0-9_-]{12,}", "[REDACTED]", message)
        result = {"passed": False, "offline_self_test_passed": False, "model_mode": "real_deepseek_api" if args.live else "offline_scripted_self_test",
                  "failure": f"{type(error).__name__}: {message[:250]}", "checks": {"complete_run": False}}
    write_report(folder, result)
    print("V9.3 report:", folder / "result.md")
    print("result:", "PASS" if result["passed"] else "OFFLINE SELF TEST" if result["offline_self_test_passed"] else "FAIL")
    if not result["passed"] and (args.live or not result["offline_self_test_passed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
