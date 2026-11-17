"""V10.0：从已经落盘的 V1～V9 事实构建只读组件证据图，不臆测调用。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def _read(path: Path, default: Any = None) -> Any:
    """只读取磁盘文件，不初始化 Store，不触碰业务状态；损坏则显式报错。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"状态文件无法读取：{path.name}: {type(error).__name__}") from error


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """按行解析追加日志，文件不存在表示未观测，损坏行不被默默跳过。"""
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"事件日志无效：{path.name}: {type(error).__name__}") from error


class ComponentGraph:
    """把原生状态文件关联成可检查证据链，未检查网络远端和模型内部意图。"""

    def __init__(self, repo: Path, state: Path) -> None:
        """绑定可信仓库与外部状态目录，不做隐式磁盘写入。"""
        self.repo = Path(repo).resolve(strict=True)
        self.state = Path(state).resolve()
        if self.state == self.repo or self.state.is_relative_to(self.repo):
            raise ValueError("Component state must stay outside the Git repository")

    def inspect(self, run_id: str) -> dict[str, Any]:
        """验证 Task-Job-Event-Notice-Goal 关联，返回可供 CLI、Chat 和 Benchmark 使用的摘要。"""
        if not _ID.fullmatch(run_id):
            raise ValueError("Invalid Workflow run ID")
        session = _read(self.state / "sessions" / f"{run_id}.json")
        wf = _read(self.state / "workflows" / f"{run_id}.json")
        if not isinstance(session, dict) or not isinstance(wf, dict) or wf.get("id") != run_id:
            raise FileNotFoundError("Session and Workflow files must exist and match the run ID")
        if session.get("run_id") != run_id or not isinstance(session.get("repo"), str) or not session["repo"] or Path(session["repo"]).resolve() != self.repo:
            raise PermissionError("Session does not belong to the selected repository")
        steps = wf.get("steps")
        if not isinstance(steps, dict):
            raise ValueError("Workflow steps are not a JSON object")
        components: dict[str, dict[str, Any]] = {}
        checks: list[dict[str, Any]] = []
        findings: list[str] = []

        def component(name: str, observed: bool, evidence: str = "", *, note: str = "") -> None:
            """只把有直接证据的组件标记为 observed；不以文件存在推断动作已运行。"""
            components[name] = {"status": "observed" if observed else "not_observed", "evidence": evidence, "note": note}

        component("workflow", True, "workflows/<run_id>.json")
        component("session", True, "sessions/<run_id>.json")
        probe = session.get("v10_probe") or {}
        if not isinstance(probe, dict):
            probe = {}
        component("context_prepare", int(probe.get("context_prepares", 0)) > 0, "session.v10_probe.context_prepares")
        component("memory_recall", int(probe.get("memory_recalls", 0)) > 0, "session.v10_probe.memory_recalls",
                  note="调用过搜索方法，不保证找到相关知识")
        called = probe.get("tools_called") if isinstance(probe.get("tools_called"), dict) else {}
        component("tool_dispatch", bool(called), "session.v10_probe.tools_called",
                  note="记录工具调用尝试，返回不代表写入成功")
        component("skill_load", bool(called.get("load_skill")), "session.v10_probe.tools_called.load_skill")
        component("mcp_tool_call", any(x.startswith("mcp__") and n for x, n in called.items()),
                  "session.v10_probe.tools_called", note="只证明 MCP 工具被调用，不证明外部副作用")
        audit = _jsonl(self.state / "component_audit" / f"{run_id}.jsonl")
        component("model_analysis", any(x.get("event") == "lead_analyzed" for x in audit),
                  "component_audit/<run_id>.jsonl")
        team_id = session.get("source_worker_run_id") or run_id
        if not _ID.fullmatch(str(team_id)):
            raise ValueError("Invalid source Worker workflow ID")
        team_root = self.state / "team" / team_id
        tasks = _read(team_root / "tasks.json", [])
        jobs = _read(team_root / "jobs.json", [])
        notices = _read(team_root / "lead_notices.json", [])
        cursor = _read(team_root / "lead_cursor.json", {})
        events = _jsonl(team_root / "events.jsonl")
        if not all(isinstance(x, list) for x in (tasks, jobs, notices, events)):
            raise ValueError("Invalid team Task, Job, Event or Notice store shape")
        tasks_by_id = {x["id"]: x for x in tasks if isinstance(x, dict) and isinstance(x.get("id"), str)}
        jobs_by_id = {x["id"]: x for x in jobs if isinstance(x, dict) and isinstance(x.get("id"), str)}
        published = {x["event"]["id"]: x["event"] for x in events
                     if isinstance(x, dict) and x.get("kind") == "published" and isinstance(x.get("event"), dict)
                     and isinstance(x["event"].get("id"), str)}
        acked = {x["event_id"] for x in events if isinstance(x, dict) and x.get("kind") == "acked"
                 and isinstance(x.get("event_id"), str)}
        notice_by_job: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for index, notice in enumerate(notices):
            if isinstance(notice, dict) and isinstance(notice.get("job_id"), str):
                notice_by_job[notice["job_id"]].append((index, notice))
        consumed = cursor.get("position", 0) if isinstance(cursor, dict) else 0
        if type(consumed) is not int or consumed < 0:
            raise ValueError("Invalid Lead cursor")
        if consumed > len(notices):
            findings.append("Lead cursor exceeds notice count")

        original_steps = steps
        if team_id != run_id:
            source_wf = _read(self.state / "workflows" / f"{team_id}.json", {})
            original_steps = source_wf.get("steps", {}) if isinstance(source_wf, dict) else {}
        plan = session.get("plan") if isinstance(session.get("plan"), dict) else {}
        planned = plan.get("workers", [])
        if not isinstance(planned, list):
            planned = []
        for worker in planned:
            if not isinstance(worker, dict) or not isinstance(worker.get("id"), str):
                continue
            wid = worker["id"]
            info = original_steps.get(wid, {}) if isinstance(original_steps, dict) else {}
            if info.get("status") != "succeeded":
                continue  # 尚未执行的 Worker 不是断链
            output = info.get("output") or {}
            if not isinstance(output, dict):
                findings.append(f"Worker {wid} output missing")
                continue
            task_id, job_id = output.get("task_id"), output.get("job_id")
            task, job = tasks_by_id.get(task_id), jobs_by_id.get(job_id)
            eid = f"event_{job_id}" if isinstance(job_id, str) else ""
            ev = published.get(eid)
            items = notice_by_job.get(job_id, [])
            chain = {
                "worker_step": wid, "task_id": task_id, "job_id": job_id,
                "worktree_branch_recorded": isinstance(output.get("branch"), str) and
                                            output["branch"].startswith("devpilot/") and
                                            ".." not in output["branch"],
                "task_exists": task is not None,
                "job_exists": job is not None,
                "job_links_task": bool(job and job.get("task_id") == task_id),
                "job_completed": bool(job and job.get("status") == "completed"),
                "event_published": bool(ev and ev.get("job_id") == job_id and ev.get("task_id") == task_id
                                         and ev.get("type") == "job.completed"),
                "event_acked": eid in acked,
                "notice_matches": any(n.get("event_id") == eid and n.get("task_id") == task_id for _, n in items),
                "lead_consumed": any(index < consumed and n.get("event_id") == eid for index, n in items),
            }
            chain["linked"] = all(value for key, value in chain.items() if key.endswith(("_exists", "_task", "_completed", "_published", "_acked", "_matches", "_consumed")))
            checks.append(chain)
            if not chain["linked"]:
                findings.append(f"Worker {wid}: persisted Task/Job/Event/Lead chain incomplete")
        component("task", any(c["task_exists"] for c in checks), "team/<source_run>/tasks.json")
        component("background_job", any(c["job_completed"] for c in checks), "team/<source_run>/jobs.json")
        component("event_publish", any(c["event_published"] for c in checks), "team/<source_run>/events.jsonl")
        component("event_ack", any(c["event_acked"] for c in checks), "team/<source_run>/events.jsonl")
        component("lead_notice", any(c["notice_matches"] for c in checks), "team/<source_run>/lead_notices.json")
        component("lead_consumption", any(c["lead_consumed"] for c in checks), "team/<source_run>/lead_cursor.json")
        component("worker_worktree", any(c.get("linked") and c.get("worktree_branch_recorded") for c in checks),
                  "worker step output.branch + Task/Job/Event chain",
                  note="分支记录能证明 Worker 关联过分支，无法证明 Worktree 目录目前仍存在")

        goal_id = session.get("goal_id")
        goal = _read(self.state / "goal" / "goals" / f"{goal_id}.json") if isinstance(goal_id, str) and _ID.fullmatch(goal_id) else None
        verified_step = steps.get("verify") if isinstance(steps.get("verify"), dict) else {}
        mode = session.get("mode", "edit")
        criteria = goal.get("criteria") if isinstance(goal, dict) else None
        criteria_passed = isinstance(criteria, dict) and bool(criteria) and all(
            isinstance(row, dict) and row.get("status") == "pass" for row in criteria.values()
        )
        goal_ok = bool(isinstance(goal, dict) and goal.get("id") == goal_id and
                       goal.get("status") == "verified" and criteria_passed)
        if mode in {"repair", "repair_delivery"}:
            # V8 返工的重新验证由 GoalApplication 执行，不一定有 V7 verify 步骤。
            source = _read(self.state / "workflows" / f"{team_id}.json", {})
            source_ok = isinstance(source, dict) and source.get("status") == "completed"
            goal_verified = goal_ok and source_ok and (
                mode == "repair" or (steps.get("delivery_review", {}).get("status") == "succeeded")
            )
        else:
            goal_verified = goal_ok and verified_step.get("status") == "succeeded" and (
                (verified_step.get("output") or {}).get("status") == "verified"
            )
        component("goal_verified", goal_verified, "goal/goals/<goal_id>.json + workflows/<run_id>.json",
                  note="需 Goal verified、验收条件全部 pass，且正确关联原开发或返工 Workflow")
        if verified_step.get("status") == "succeeded" and not goal_verified:
            findings.append("Workflow verify succeeded but Goal/criteria evidence is missing or not verified")
        if mode == "repair_delivery" and steps.get("report", {}).get("status") == "succeeded" and not goal_verified:
            findings.append("Repair delivery report succeeded without linked verified Goal evidence")
        report = _read(self.state / "reports" / f"{run_id}.json")
        report_step = steps.get("report") if isinstance(steps.get("report"), dict) else {}
        component("delivery_report", isinstance(report, dict) and report_step.get("status") == "succeeded" and report.get("run_id") == run_id,
                  "reports/<run_id>.json + workflow.report")
        pr = report.get("github_pr") if isinstance(report, dict) else None
        component("pr_receipt", isinstance(pr, dict) and bool(pr.get("url")), "reports/<run_id>.json.github_pr",
                  note="本地回执；若要证明 GitHub 远端状态，请使用 PR Recovery")
        component("cron_executed", any(x.get("event") == "cron_fired" for x in audit),
                  "component_audit/<run_id>.jsonl", note="CronStore 存在不等于已运行")
        component("mailbox_traffic", any(x.get("event") == "mailbox_message_sent" for x in audit),
                  "component_audit/<run_id>.jsonl", note="Mailbox 文件存在不等于发送过消息")
        return {
            "schema_version": "v10.0.0", "run_id": run_id, "workflow_status": wf.get("status"),
            "goal_status": goal.get("status") if isinstance(goal, dict) else None,
            "source_worker_run_id": team_id, "worker_chains": checks, "components": components,
            "findings": findings,
            "observed_count": sum(v["status"] == "observed" for v in components.values()),
            "component_count": len(components),
            "all_worker_chains_linked": all(c["linked"] for c in checks) if checks else None,
            "note": "仅证实已检查的本地持久化证据；不等于完整端到端成功或远端 GitHub 状态。",
        }
