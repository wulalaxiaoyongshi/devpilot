"""生成包含目标、可信证据、Git 版本、审批和预算的可审计交付快照。"""

import json
import subprocess
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .application import GoalApplication
from .verifier import GoalVerifier

_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class DeliveryCriterion:
    """保存一条验收条件及其结果、证据编号和检查原因。"""

    criterion_id: str
    description: str
    required: bool
    status: str
    evidence_id: str | None
    reason: str
    observed: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRepair:
    """保存一次修复的编号、运行状态和所用 V7 工作流。"""

    ticket_id: str
    round_number: int
    status: str
    workflow_run_id: str | None
    workflow_status: str | None
    commits: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalDeliveryReport:
    """保存一次完整交付检查的不可变 JSON 快照而不冒充绝对质量保证。"""

    id: str
    goal_run_id: str
    goal_version: int
    goal_title: str
    goal_description: str
    goal_status: str
    delivery_status: str
    generated_at: str
    git_head: str | None
    git_clean: bool
    criteria: tuple[DeliveryCriterion, ...]
    repairs: tuple[DeliveryRepair, ...]
    used_rounds: int
    used_tokens: int
    used_cost_usd: float
    reviewer: str | None
    warnings: tuple[str, ...]
    changed_files: tuple[str, ...] = ()


class GoalDeliveryBuilder:
    """从已持久化的 Goal、Evidence、V7 Workflow 与预算生成最终报告。"""

    def __init__(self, app: GoalApplication, *, require_approval: bool = True) -> None:
        """保存统一 Goal 入口，并设置是否强制要求 V7 有真实审批记录。"""
        if type(require_approval) is not bool:
            raise TypeError("require_approval must be boolean")
        self.app = app
        self.require_approval = require_approval

    def _fresh_verification(self, goal, head: str | None, dirty: bool | None) -> list[str]:
        """重新核对所有必要证据的来源、Git 版本和最新结果，防止旧结论误交付。"""
        problems = []
        if head is None or dirty is not False:
            problems.append("当前 Git HEAD 不可确认或工作区存在未提交修改")
            return problems
        latest = {record.criterion_id: record for record in self.app.evidence_store.list_for_goal(goal.id)}
        spec_hash = GoalVerifier._spec_hash(goal)
        since = GoalVerifier._since(goal)
        for criterion in goal.spec.criteria:
            if not criterion.required:
                continue
            accepted = goal.criteria[criterion.id]
            record = latest.get(criterion.id)
            if accepted.status != "pass" or not accepted.evidence_ids or record is None:
                problems.append(f"验收条件 {criterion.id} 没有可靠的 PASS 证据")
                continue
            if record.id != accepted.evidence_ids[-1]:
                problems.append(f"验收条件 {criterion.id} 存在未经复核的新证据")
                continue
            issue = self.app.verifier._provenance_problem(record, criterion, goal, spec_hash, head, dirty, since)
            if issue is not None:
                problems.append(f"验收条件 {criterion.id} 证据失效：{issue}")
                continue
            verdict, _ = self.app.verifier._compare(criterion, record.observation)
            if verdict != "pass":
                problems.append(f"验收条件 {criterion.id} 当前证据不能证明通过")
        return problems

    def _approval_actor(self, repairs: tuple[DeliveryRepair, ...]) -> str | None:
        """只认可最后一轮已完成修复所关联的 V7 审批，不复用上轮批准。"""
        if not repairs or repairs[-1].workflow_status != "completed" or repairs[-1].workflow_run_id is None:
            return None
        workflow = self.app.workflow.store.get(repairs[-1].workflow_run_id)
        for step in workflow.definition.steps:
            state = workflow.steps[step.id]
            request = state.approval_request
            if step.kind == "approval" and request is not None and request.status == "approved" and request.decided_by:
                return request.decided_by
        return None

    def _changed_files(self, repairs: tuple[DeliveryRepair, ...], head: str | None) -> tuple[str, ...]:
        """从真实 Git 中核对修复步骤上报的 Commit 并收集实际修改的文件名。"""
        if head is None:
            return ()
        files = set()
        for repair in repairs:
            for commit in repair.commits:
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    continue
                try:
                    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, head], cwd=self.app.verifier.workspace,
                                              capture_output=True, timeout=5, check=False)
                    if ancestor.returncode != 0:
                        continue
                    changed = subprocess.run(["git", "show", "--format=", "--name-only", commit], cwd=self.app.verifier.workspace,
                                             capture_output=True, text=True, timeout=5, check=False)
                    if changed.returncode == 0:
                        files.update(item.strip() for item in changed.stdout.splitlines() if item.strip())
                except (OSError, subprocess.TimeoutExpired):
                    continue
        return tuple(sorted(files))

    def build(self, goal_run_id: str) -> GoalDeliveryReport:
        """重新核对代码和审批，再保存一份能够解释不予交付原因的验收快照。"""
        goal = self.app.get(goal_run_id)
        head, dirty = self.app.verifier._git_state()
        latest_evidence = {record.criterion_id: record for record in self.app.evidence_store.list_for_goal(goal_run_id)}
        criterion_rows = tuple(
            DeliveryCriterion(item.id, item.description, item.required,
                              goal.criteria[item.id].status,
                              goal.criteria[item.id].evidence_ids[-1] if goal.criteria[item.id].evidence_ids else None,
                              goal.criteria[item.id].reason,
                              {key: value for key, value in latest_evidence[item.id].observation.items()
                               if key in {"returncode", "argv", "sha256", "exists", "contains_target", "stdout_sha256", "stderr_sha256"}}
                              if item.id in latest_evidence else {})
            for item in goal.spec.criteria
        )
        ticket_rows = []
        for ticket in self.app.repair_store.list_for_goal(goal_run_id):
            status = self.app.workflow.store.get(ticket.workflow_run_id).status if ticket.workflow_run_id else None
            commits = ()
            if ticket.workflow_run_id:
                workflow = self.app.workflow.store.get(ticket.workflow_run_id)
                commits = tuple(step.output["commit"] for step in workflow.steps.values()
                                if isinstance(step.output, dict) and isinstance(step.output.get("commit"), str)
                                and re.fullmatch(r"[0-9a-f]{40}", step.output["commit"]))
            ticket_rows.append(DeliveryRepair(ticket.id, ticket.round_number, ticket.status, ticket.workflow_run_id, status, commits))
        repairs = tuple(ticket_rows)
        account = self.app.budget_store.get(goal_run_id)
        warnings = []
        reviewer = self._approval_actor(repairs)
        if goal.status != "verified":
            status = "not_verified"
            warnings.append(f"Goal 状态是 {goal.status}，尚未达到自动验收通过")
        else:
            warnings.extend(self._fresh_verification(goal, head, dirty))
            if warnings:
                status = "verification_stale"
            elif self.require_approval and reviewer is None:
                status = "awaiting_review"
                warnings.append("未找到与该 Goal 修复流程关联的有效人工批准记录")
            else:
                status = "ready_for_delivery"
        return GoalDeliveryReport(
            uuid4().hex, goal.id, goal.version, goal.spec.title, goal.spec.description,
            goal.status, status, datetime.now(timezone.utc).isoformat(), head, dirty is False,
            criterion_rows, repairs, len(repairs), sum(event.tokens for event in account.events),
            round(sum(event.cost_usd for event in account.events), 9), reviewer, tuple(warnings),
            self._changed_files(repairs, head),
        )

    @staticmethod
    def render_markdown(report: GoalDeliveryReport) -> str:
        """将已检查的事实整理成适合用户阅读的 Markdown，而不注入执行指令。"""
        def safe(value: object) -> str:
            """将用户和模型提供的文本折叠为安全、有限的表格单元格。"""
            return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")[:500]
        lines = ["# DevPilot Goal 交付报告", "",
                 f"**目标：** {safe(report.goal_title)}", f"**Goal ID：** `{report.goal_run_id}`",
                 f"**Goal 状态：** `{report.goal_status}`", f"**交付状态：** `{report.delivery_status}`",
                 f"**Git HEAD：** `{report.git_head or 'UNKNOWN'}`", f"**工作区干净：** {report.git_clean}",
                 f"**生成时间：** {report.generated_at}", "", "## 验收结果", "",
                 "| 条件 | 必须 | 结论 | 证据 ID | 原因 |", "|---|---|---|---|---|",]
        for item in report.criteria:
            lines.append(f"| {safe(item.description)} | {'是' if item.required else '否'} | {item.status} | {item.evidence_id or '无'} | {safe(item.reason)} |")
        lines.extend(["", "## 修复记录", "", "| 轮次 | 修复单 | 工作流 | 状态 |", "|---|---|---|---|"])
        for item in report.repairs:
            lines.append(f"| {item.round_number} | `{item.ticket_id}` | `{item.workflow_run_id or '无'}` | {item.status} / {item.workflow_status or '未提交'} |")
        lines.extend(["", "## 代码变更", ""])
        if report.changed_files:
            lines.extend(f"- `{safe(item)}`" for item in report.changed_files)
        else:
            lines.append("- 尚无经过 Git 核实的 Commit 文件记录")
        lines.extend(["", "## 测试与文件证据摘要", ""])
        for item in report.criteria:
            if item.observed:
                lines.append(f"- {safe(item.criterion_id)}：`{safe(json.dumps(item.observed, ensure_ascii=False))}`")
        lines.extend(["", "## 用量与审批", "",
 f"- 修复轮次：{report.used_rounds}",
                      f"- 宿主上报 Token：{report.used_tokens}", f"- 宿主上报费用（USD）：{report.used_cost_usd}",
                      f"- 审批人：{safe(report.reviewer or '暂无有效审批')}"])
        if report.warnings:
            lines.extend(["", "## 不能交付的原因", ""])
            lines.extend(f"- {safe(item)}" for item in report.warnings)
        lines.extend(["", "本报告只证明已配置验收标准及其记录，不保证软件不存在其他问题。", ""])
        return "\n".join(lines)


class GoalReportStore:
    """按 Goal 独立保存历史 JSON 报告和可读 Markdown，而不是覆盖最后一份结果。"""

    def __init__(self, root: str | Path) -> None:
        """创建可信报告存储目录，并保证一个实例内的写入互斥。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, goal_id: str, report_id: str, suffix: str = ".json") -> Path:
        """检查文件编号，只允许在指定 Goal 的报告目录下生成文件。"""
        if not all(isinstance(item, str) and _ID.fullmatch(item) for item in (goal_id, report_id)):
            raise ValueError("Invalid goal or report ID")
        return self.root / goal_id / f"{report_id}{suffix}"

    @staticmethod
    def _atomic_new(path: Path, contents: str) -> None:
        """先落盘同目录临时文件再发布新报告，不允许覆盖已有同名记录。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"Report already exists: {path.name}")
        fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                raise FileExistsError(f"Report already exists: {path.name}")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def append(self, report: GoalDeliveryReport) -> tuple[Path, Path]:
        """追加不可覆盖的 JSON 快照，并导出同编号 Markdown 供用户阅读。"""
        with self._lock:
            json_path = self._path(report.goal_run_id, report.id)
            md_path = self._path(report.goal_run_id, report.id, ".md")
            if json_path.exists() or md_path.exists():
                raise FileExistsError("Report ID already exists")
            self._atomic_new(json_path, json.dumps(asdict(report), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
            self._atomic_new(md_path, GoalDeliveryBuilder.render_markdown(report))
            return json_path, md_path

    def get(self, goal_run_id: str, report_id: str) -> GoalDeliveryReport:
        """从持久化 JSON 恢复历史交付报告并核对记录所属的目标。"""
        path = self._path(goal_run_id, report_id)
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        result = GoalDeliveryReport(**{**raw,
            "criteria": tuple(DeliveryCriterion(**item) for item in raw["criteria"]),
            "repairs": tuple(DeliveryRepair(**{**item, "commits": tuple(item.get("commits", []))}) for item in raw["repairs"]),
            "warnings": tuple(raw["warnings"]),
            "changed_files": tuple(raw.get("changed_files", [])),
        })
        if result.id != report_id or result.goal_run_id != goal_run_id:
            raise ValueError("Report record identity mismatch")
        return result

    def list_for_goal(self, goal_run_id: str) -> tuple[GoalDeliveryReport, ...]:
        """按生成时间读取某个目标的历史报告，让 UI 展示审计记录。"""
        self._path(goal_run_id, "0" * 32)
        folder = self.root / goal_run_id
        if not folder.exists():
            return ()
        return tuple(sorted((self.get(goal_run_id, path.stem) for path in folder.glob("[0-9a-f]" * 32 + ".json")),
                            key=lambda row: (row.generated_at, row.id)))
