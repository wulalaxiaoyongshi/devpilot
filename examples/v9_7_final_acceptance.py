"""V9.7：用不同真实 Git 项目和安全失败场景验收 V0～V8 的统一 Coding Agent。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from devpilot.core.message import ToolCall
from devpilot.core.model import DeepSeekModel, ModelResponse
from devpilot.runtime.acceptance import build_final_report, case_passed, safe_message, write_final_report
from devpilot.runtime.general_coder import GeneralCodingAgent, git
from devpilot.runtime.lead_analysis import create_lead_analysis_app
from devpilot.runtime.general_plan import parse_coding_plan


@dataclass(frozen=True)
class Scenario:
    """定义一类 Python 开发任务需要的真实代码、测试和受控模型样本。"""

    name: str
    source: str
    broken: str
    fixed: str
    test_file: str
    test_content: str
    task: str
    plan: dict[str, Any]
    edits: dict[str, tuple[str, str]]
    read_marker: str


def scenarios() -> dict[str, Scenario]:
    """提供 Bug 修复和新功能开发两种互不相同的隔离 Git 验收任务。"""
    return {
        "bugfix": Scenario(
            "bugfix", "mathbox.py", "def add(a, b):\n    return a - b\n",
            "def add(a, b):\n    return a + b\n", "test_mathbox.py",
            "from mathbox import add\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 3) == 2\n",
            "修复 mathbox.py 的 add 函数，它错误地做了减法，必须保持 test_mathbox.py 原样并通过 pytest。",
            {"summary": "修复整数加法", "workers": [
                {"id": "repair", "task": "将 mathbox.py 的 add 改成正确加法", "files": ["mathbox.py"]}]},
            {"repair": ("mathbox.py", "def add(a, b):\n    return a + b\n")}, "def add",
        ),
        "feature": Scenario(
            "feature", "slug.py", "def slugify(value):\n    raise NotImplementedError\n",
            "import re\n\ndef slugify(value):\n    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', value.lower())).strip('-')\n",
            "test_slug.py",
            "from slug import slugify\n\ndef test_slug():\n    assert slugify('Hello World') == 'hello-world'\n    assert slugify(' Already--Done ') == 'already-done'\n",
            "实现 slug.py 的 slugify 文本转 URL 名称功能，保持 test_slug.py 不变并通过 pytest；可以新增独立 helper.py。",
            {"summary": "实现 slugify 并新增辅助模块", "workers": [
                {"id": "implement", "task": "在 slug.py 实现 slugify", "files": ["slug.py"]},
                {"id": "support", "task": "在 helper.py 新增 is_slug()", "files": ["helper.py"]}]},
            {"implement": ("slug.py", "import re\n\ndef slugify(value):\n    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', value.lower())).strip('-')\n"),
             "support": ("helper.py", "def is_slug(text):\n    return bool(text) and text == text.lower() and ' ' not in text\n")},
            "def slugify",
        ),
    }


def make_git_fixture(repo: Path, scenario: Scenario) -> None:
    """只在全新目录中初始化真实 Git 仓库，并写入必然失败的回归测试。"""
    if repo.exists():
        raise FileExistsError(f"Refusing to overwrite acceptance repository: {repo}")
    repo.mkdir(parents=True)
    (repo / scenario.source).write_text(scenario.broken, encoding="utf-8")
    (repo / scenario.test_file).write_text(scenario.test_content, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    for argv in (("init",), ("config", "user.name", "DevPilot V9.7 Acceptance"),
                 ("config", "user.email", "acceptance@example.invalid"),
                 ("add", "-A"), ("commit", "-m", "broken scenario fixture")):
        subprocess.run(("git", *argv), cwd=repo, capture_output=True, text=True, check=True, timeout=20)


class OfflineScenarioModel:
    """仅伪造模型回复，真实 V0～V8、Git、后台任务和 pytest 仍调用原项目实现。"""

    def __init__(self, role: str, scenario: Scenario):
        """按模型身份保存对应场景的受控 ToolCall 与计划数据。"""
        self.role, self.scenario, self.count = role, scenario, 0

    def complete(self, messages: Any, tools: Any = None, system: Any = None) -> ModelResponse:
        """分析时读取真实源码，规划时输出 JSON，执行时修改获授权的目标文件。"""
        self.count += 1
        if self.role == "planner":
            return ModelResponse(json.dumps(self.scenario.plan, ensure_ascii=False))
        if self.role == "lead":
            if self.count == 1:
                return ModelResponse("", [ToolCall("read_1", "read_file", {"path": self.scenario.source})])
            return ModelResponse(f"分析了 {self.scenario.source}，确认需要按真实测试进行修复。")
        worker_id = self.role.rsplit("_", 1)[-1]
        if worker_id not in self.scenario.edits:
            raise RuntimeError(f"Offline scenario has no permitted worker: {worker_id}")
        filename, content = self.scenario.edits[worker_id]
        if self.count == 1:
            return ModelResponse("", [ToolCall("write_1", "write_file", {"path": filename, "content": content})])
        return ModelResponse("Worker wrote the approved file")


class TraceModel:
    """记录真实或离线模型的请求与工具结果，不在验收文件中保存消息正文或密钥。"""

    def __init__(self, inner: Any, role: str, traces: dict[str, dict[str, Any]], read_marker: str):
        """保存底层模型和调用追踪容器。"""
        self.inner, self.role, self.traces, self.read_marker = inner, role, traces, read_marker
        traces.setdefault(role, {"calls": 0, "requested_tools": [], "tool_result_ids": [],
                                 "read_result_confirmed": False, "write_result_confirmed": False,
                                 "mcp_result_confirmed": False})

    def complete(self, messages: Any, tools: Any = None, system: Any = None) -> ModelResponse:
        """实际调用模型并检测 ToolResult 是否返回源码、写入确认或 MCP 结果。"""
        row = self.traces[self.role]
        for message in messages:
            if message.role != "tool" or message.tool_call_id in row["tool_result_ids"]:
                continue
            row["tool_result_ids"].append(message.tool_call_id)
            content = message.content or ""
            row["read_result_confirmed"] |= self.read_marker in content and not content.startswith("Error:")
            row["write_result_confirmed"] |= ("Wrote" in content or "Successfully" in content) and not content.startswith("Error:")
            row["mcp_result_confirmed"] |= "[MCP RESULT:" in content
        response = self.inner.complete(messages, tools=tools, system=system)
        row["calls"] += 1
        row["requested_tools"].extend(call.name for call in response.tool_calls)
        return response


def model_factory(scenario: Scenario, live: bool, model_name: str, traces: dict[str, dict[str, Any]]) -> Callable[[str], Any]:
    """离线时采用受控脚本模型，在线时必须实例化真正的 DeepSeekModel。"""
    def factory(role: str) -> TraceModel:
        """为每个 Lead、Planner、Worker 创建独立的模型追踪器。"""
        inner = DeepSeekModel(model_name) if live else OfflineScenarioModel(role, scenario)
        return TraceModel(inner, role, traces, scenario.read_marker)
    return factory


def _model_checks(traces: dict[str, dict[str, Any]], *, require_write: bool) -> dict[str, bool]:
    """独立检查模型是否真实产生读写 ToolCall 且收到了对应执行结果。"""
    leader = traces.get("lead", {})
    worker_rows = [row for role, row in traces.items() if role not in {"lead", "planner"}]
    result = {
        "lead_used_read_file": "read_file" in leader.get("requested_tools", []),
        "lead_received_source": bool(leader.get("read_result_confirmed")),
        "model_calls_recorded": sum(row["calls"] for row in traces.values()) > 0,
    }
    if require_write:
        result.update({
            "worker_used_write_file": any("write_file" in row["requested_tools"] for row in worker_rows),
            "worker_received_write_result": any(row["write_result_confirmed"] for row in worker_rows),
        })
    return result


def run_analysis(directory: Path, *, live: bool, model_name: str, mcp_config: Path | None = None,
                 mcp_aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    """通过真实 V0～V3 分析入口读取仓库，并确认只读分析没有产生任何 Git 修改。"""
    case = scenarios()["bugfix"]
    repo = directory / "repo"
    make_git_fixture(repo, case)
    traces: dict[str, dict[str, Any]] = {}
    factory = model_factory(case, live, model_name, traces)
    checks: dict[str, bool] = {}
    error = None
    try:
        with create_lead_analysis_app(repo, factory, state_dir=directory / "analysis_state") as app:
            response = app.analyze("请先实际调用 read_file 阅读 mathbox.py，指出 add 的错误并描述修复计划；不准改代码。")
            checks["analysis_reply"] = bool(response.strip())
            checks["forbidden_tools_absent"] = not {"write_file", "bash", "edit_file"}.intersection(app.tools())
        checks.update(_model_checks(traces, require_write=False))
        checks["repository_unchanged"] = git(repo, "status", "--porcelain", "--untracked-files=all") == ""
        checks["source_still_broken"] = (repo / case.source).read_text(encoding="utf-8") == case.broken
        if mcp_config is not None:
            checks["mcp_checked_in_coding_cases"] = True
    except Exception as exc:
        error = safe_message(exc)
    return {"name": "analysis", "checks": checks, "error": error,
            "real_model_calls": sum(row["calls"] for row in traces.values()) if live else 0,
            "model_trace": traces, "mcp_verified": False}


def run_coding_case(directory: Path, case: Scenario, *, live: bool, model_name: str,
                    authorize_fixture_approval: bool, mcp_config: Path | None = None,
                    mcp_aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    """在独立 Git 仓库中验证计划审批、真实 Worker、Git 合并、Goal 和重启后交付。"""
    repo = directory / "repo"
    make_git_fixture(repo, case)
    traces: dict[str, dict[str, Any]] = {}
    factory = model_factory(case, live, model_name, traces)
    checks: dict[str, bool] = {}
    error = None
    run_id = None
    baseline = git(repo, "rev-parse", "HEAD")
    try:
        with GeneralCodingAgent(repo, directory / "state", factory, test_mode="pytest",
                                mcp_config=mcp_config, mcp_read_aliases=mcp_aliases) as app:
            mcp_hint = (" 请先调用已授权 MCP Server（别名：" + ", ".join(mcp_aliases) +
                        "）提供的只读查询工具，实际获取结果后再分析本地代码；"
                        "若为 fixture Server，则调用 get_issue(issue_number=15)。") if mcp_config else ""
            start = app.create(case.task + mcp_hint)
            run_id = start["run_id"]
            checks["preapproval_required"] = start["workflow"] == "waiting_approval"
            checks["nothing_changed_before_approval"] = git(repo, "rev-parse", "HEAD") == baseline and git(repo, "status", "--porcelain") == ""
            plan = app.session(run_id)["plan"]
            allowed_targets = {filename for worker in plan["workers"] for filename in worker["files"]}
            checks["test_file_not_editable"] = case.test_file not in allowed_targets
            checks["target_file_planned"] = case.source in allowed_targets
            checks["plan_dynamically_validated"] = (parse_coding_plan(json.dumps(plan), repo) == plan)
            if authorize_fixture_approval and all(checks.values()):
                approval = start["approvals"][0]
                app.approve(run_id, approval["step"], approval["request_id"], actor="reviewer", approved=True)
                progressed = app.advance(run_id, timeout_seconds=120)
                checks["goal_verified"] = progressed["goal"] == "verified"
                checks["delivery_approval_required"] = progressed["workflow"] == "waiting_approval"
                checks["no_report_before_final_approval"] = progressed["report"] is None
                changed = git(repo, "diff", "--name-only", baseline, "HEAD").splitlines()
                checks["git_change_scoped"] = case.source in changed and set(changed).issubset(allowed_targets)
                checks["test_file_unchanged"] = case.test_file not in changed
                checks["code_behaviour_changed"] = (repo / case.source).read_text(encoding="utf-8") != case.broken
                checks["git_commit_created"] = git(repo, "rev-parse", "HEAD") != baseline
                checks["git_clean"] = git(repo, "status", "--porcelain", "--untracked-files=all") == ""
                jobs = app._team_app.jobs() if app._team_app else []
                notices = app._team_app.core.delivery.notices.all() if app._team_app else []
                checks["real_v5_jobs_completed"] = len(jobs) == len(plan["workers"]) and all(job.status == "completed" for job in jobs)
                checks["v5_events_persisted"] = {job.id for job in jobs} == {notice.job_id for notice in notices}
                checks["lead_consumed_notifications"] = bool(notices) and app._team_app.core.delivery.cursor.read() == len(notices)
        if authorize_fixture_approval and all(checks.values()) and run_id is not None:
            with GeneralCodingAgent(repo, directory / "state", factory, test_mode="pytest") as reopened:
                pending = reopened.status(run_id)
                checks["restart_retained_approval"] = pending["workflow"] == "waiting_approval" and len(pending["approvals"]) == 1
                if checks["restart_retained_approval"]:
                    review = pending["approvals"][0]
                    reopened.approve(run_id, review["step"], review["request_id"], actor="reviewer", approved=True)
                    final = reopened.advance(run_id, timeout_seconds=45)
                    checks["workflow_completed"] = final["workflow"] == "completed"
                    checks["report_written"] = bool(final["report"]) and Path(final["report"]).is_file()
                    if checks["report_written"]:
                        doc = json.loads((directory / "state" / "reports" / f"{run_id}.json").read_text(encoding="utf-8"))
                        checks["report_has_goal_evidence"] = doc["goal"]["goal_status"] == "verified" and doc["status"] == "ready_for_delivery"
                        checks["report_tracks_git_head"] = doc["git_head"] == git(repo, "rev-parse", "HEAD")
        checks.update(_model_checks(traces, require_write=authorize_fixture_approval))
        if mcp_config is not None:
            checks["real_mcp_result_seen"] = any(row["mcp_result_confirmed"] for row in traces.values())
    except Exception as exc:
        error = safe_message(exc)
    return {"name": case.name, "checks": checks, "error": error,
            "real_model_calls": sum(row["calls"] for row in traces.values()) if live else 0,
            "model_trace": traces, "run_id": run_id,
            "mcp_verified": any(row["mcp_result_confirmed"] for row in traces.values())}


def run_failure_guards(directory: Path) -> dict[str, Any]:
    """故意触发计划拒绝、人工拒绝、真实测试失败与过期证据，确认不会错误交付。"""
    case = scenarios()["bugfix"]
    checks: dict[str, bool] = {}
    error = None
    try:
        from devpilot.runtime.general_plan import checked_python_path
        try:
            checked_python_path(directory, "../.env")
            checks["path_traversal_denied"] = False
        except ValueError:
            checks["path_traversal_denied"] = True
        repo = directory / "rejected_repo"
        make_git_fixture(repo, case)
        with GeneralCodingAgent(repo, directory / "reject_state", model_factory(case, False, "", {})) as app:
            run = app.create("修复数学函数")
            req = run["approvals"][0]
            app.approve(run["run_id"], req["step"], req["request_id"], actor="reviewer", approved=False)
            status = app.advance(run["run_id"])
            checks["rejected_approval_blocks_work"] = status["workflow"] == "rejected" and git(repo, "status", "--porcelain") == ""
            checks["no_worker_on_rejection"] = app._team_app is None
        repo = directory / "failed_test_repo"
        make_git_fixture(repo, case)
        bad = Scenario(case.name, case.source, case.broken, case.fixed, case.test_file, case.test_content,
                       case.task, case.plan, {"repair": (case.source, case.broken + "# still wrong\n")}, case.read_marker)
        with GeneralCodingAgent(repo, directory / "fail_state", model_factory(bad, False, "", {})) as app:
            run = app.create("修复 add")
            req = run["approvals"][0]
            app.approve(run["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
            status = app.advance(run["run_id"], timeout_seconds=120)
            checks["failed_test_blocks_delivery"] = status["workflow"] == "failed" and status["goal"] != "verified"
            checks["failed_test_creates_no_report"] = status["report"] is None
        repo = directory / "stale_repo"
        make_git_fixture(repo, case)
        with GeneralCodingAgent(repo, directory / "stale_state", model_factory(case, False, "", {})) as app:
            run = app.create("修复 add")
            req = run["approvals"][0]
            app.approve(run["run_id"], req["step"], req["request_id"], actor="reviewer", approved=True)
            status = app.advance(run["run_id"], timeout_seconds=120)
            checks["stale_fixture_was_verified"] = status["goal"] == "verified"
            (repo / case.source).write_text("def add(a,b):\n    return -999\n", encoding="utf-8")
            review = status["approvals"][0]
            app.approve(run["run_id"], review["step"], review["request_id"], actor="reviewer", approved=True)
            final = app.advance(run["run_id"], timeout_seconds=30)
            checks["stale_evidence_blocks_delivery"] = final["workflow"] == "failed" and final["report"] is None
    except Exception as exc:
        error = safe_message(exc)
    return {"name": "failure_guards", "checks": checks, "error": error,
            "real_model_calls": 0, "model_trace": {}, "mcp_verified": False}


def run_suite(directory: Path, *, live: bool, authorize_fixture_approval: bool,
              model_name: str = "deepseek-v4-flash", mcp_config: Path | None = None,
              mcp_aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    """执行分析、Bug 修复、新功能和失败拦截矩阵，分别生成离线与在线最终结论。"""
    if Path(directory).exists() and any(Path(directory).iterdir()):
        raise FileExistsError("Use a new empty V9.7 acceptance output directory")
    if mcp_config is not None and not live:
        raise ValueError("Real MCP protocol acceptance requires --live and a trusted MCP config")
    if mcp_config is not None and not mcp_aliases:
        raise ValueError("MCP configuration requires --mcp-read-alias")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = [run_analysis(directory / "analysis", live=live, model_name=model_name)]
    for case in scenarios().values():
        rows.append(run_coding_case(directory / case.name, case, live=live, model_name=model_name,
                                    authorize_fixture_approval=authorize_fixture_approval,
                                    mcp_config=mcp_config, mcp_aliases=mcp_aliases))
    rows.append(run_failure_guards(directory / "failure_guards"))
    report = build_final_report("LIVE_DEEPSEEK" if live else "OFFLINE_SCRIPT_MODEL", rows,
                                requires_mcp=mcp_config is not None)
    write_final_report(directory, report)
    return report


def main(argv: list[str] | None = None) -> int:
    """提供真实模型、真实 MCP 可选验收和无需密钥的离线验收命令。"""
    parser = argparse.ArgumentParser(description="DevPilot V9.7 隔离仓库最终验收矩阵")
    parser.add_argument("--live", action="store_true", help="真实连接 DeepSeek API，不提供 Mock 模式伪装")
    parser.add_argument("--authorize-fixture-approval", action="store_true", help="仅审批本脚本新建的临时 Git 仓库")
    parser.add_argument("--output", type=Path, default=Path(".devpilot/v9_7_acceptance") / uuid4().hex)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--mcp-config", type=Path)
    parser.add_argument("--mcp-read-alias", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = run_suite(args.output, live=args.live, authorize_fixture_approval=args.authorize_fixture_approval,
                           model_name=args.model, mcp_config=args.mcp_config,
                           mcp_aliases=tuple(args.mcp_read_alias))
        print(json.dumps({"mode": result["mode"], "offline_passed": result["offline_passed"],
                          "online_passed": result["online_passed"],
                          "cases": {row["name"]: case_passed(row) for row in result["cases"]},
                          "report": str(Path(args.output).resolve() / "result.md")}, ensure_ascii=False, indent=2))
        return 0 if result["online_passed"] or result["offline_passed"] else 2
    except Exception as exc:
        print(safe_message(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
