"""提供 V8.1 Goal 生命周期入口，不冒充尚未实现的 V8.3 验证器。"""

from collections.abc import Callable
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from .model import CriterionRecord, GoalEvent, GoalRun, GoalSpec
from .store import GoalStore
from .validation import json_snapshot, validate_spec


class GoalRuntime:
    """创建目标记录并管理执行和待验证状态，但不能自行宣布验收通过。"""

    def __init__(self, store: GoalStore, clock: Callable[[], datetime] | None = None) -> None:
        """接入 GoalStore 和可注入时钟，为本实例创建状态更新锁。"""
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def _now(self) -> str:
        """获取带时区的 UTC 时间字符串，用于持久化时间戳。"""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).isoformat()

    def start(self, spec: GoalSpec, inputs: dict[str, Any] | None = None) -> GoalRun:
        """校验目标及输入，创建包含所有未验收条件的持久化 GoalRun。"""
        validate_spec(spec)
        data = json_snapshot({} if inputs is None else inputs)
        if not isinstance(data, dict):
            raise TypeError("Goal inputs must be a JSON object")
        with self._lock:
            now = self._now()
            run = GoalRun(
                id=uuid4().hex, spec=spec, inputs=data,
                criteria={criterion.id: CriterionRecord() for criterion in spec.criteria},
                created_at=now, updated_at=now,
                events=[GoalEvent(now, "created", "host", "Goal created; no criterion has been checked", "", "pending")],
            )
            return self.store.create(run)

    def get(self, run_id: str) -> GoalRun:
        """让 UI 或宿主根据 Goal ID 查询目标及每条验收条件当前的记录。"""
        return self.store.get(run_id)

    def list_goals(self, status: str | None = None) -> list[GoalRun]:
        """列出已有目标，并可按运行状态筛选。"""
        runs = self.store.list_runs()
        return runs if status is None else [run for run in runs if run.status == status]

    def _transition(self, run_id: str, allowed_from: set[str], next_status: str, event_type: str, note: str, actor: str) -> GoalRun:
        """检查状态转移是否合法，写入审计事件后以新版本保存目标。"""
        if not isinstance(note, str) or not isinstance(actor, str) or not actor.strip():
            raise ValueError("Transition requires text note and non-empty actor")
        with self._lock:
            run = self.store.get(run_id)
            if run.status not in allowed_from:
                raise ValueError(f"Cannot change {run.status} into {next_status}")
            before = run.status
            now = self._now()
            run.status = next_status
            run.updated_at = now
            run.events.append(GoalEvent(now, event_type, actor, note, before, next_status))
            return self.store.save(run)

    def begin_execution(self, run_id: str, *, actor: str = "host") -> GoalRun:
        """宿主实际准备开始执行任务时，将 pending 目标标记为 executing。"""
        return self._transition(run_id, {"pending"}, "executing", "execution_started", "Workflow execution started", actor)

    def begin_verification(self, run_id: str, *, actor: str = "host") -> GoalRun:
        """工作流完成执行后将目标标记为 verifying，等待以后 V8.3 的证据验收。"""
        return self._transition(run_id, {"executing"}, "verifying", "verification_requested", "Awaiting real verification evidence", actor)

    def resume_verification(self, run_id: str, *, actor: str = "host") -> GoalRun:
        """宿主补齐信息后让 unknown 目标重新进入验证阶段并开启新一轮证据时间窗口。"""
        return self._transition(run_id, {"unknown"}, "verifying", "verification_resumed", "Host requested fresh verification evidence", actor)

    def mark_unknown(self, run_id: str, reason: str, *, actor: str = "host") -> GoalRun:
        """遇到结果不确定时保存原因并进入 unknown，防止误报成功。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Unknown status requires a reason")
        return self._transition(run_id, {"executing", "verifying"}, "unknown", "result_unknown", reason, actor)

    def block(self, run_id: str, reason: str, *, actor: str = "host") -> GoalRun:
        """宿主确认目标暂时无法推进时标记为 blocked 并保存理由。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Blocked status requires a reason")
        return self._transition(run_id, {"pending", "executing", "verifying", "unknown"}, "blocked", "blocked", reason, actor)

    def add_note(self, run_id: str, note: str, *, actor: str = "host") -> GoalRun:
        """将一条人工备注追加到目标事件记录中，但不修改验收结论。"""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Note cannot be empty")
        with self._lock:
            run = self.store.get(run_id)
            now = self._now()
            run.updated_at = now
            run.events.append(GoalEvent(now, "note", actor, note, run.status, run.status))
            return self.store.save(run)

    def begin_repair(self, run_id: str, ticket_id: str, *, actor: str = "repair_controller") -> GoalRun:
        """只有修复单已提交给 V7 后才把验证失败的 Goal 转为 repairing。"""
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError("Repair requires a ticket ID")
        return self._transition(run_id, {"verifying"}, "repairing", "repair_started", f"Repair ticket: {ticket_id}", actor)

    def finish_repair(self, run_id: str, ticket_id: str, *, actor: str = "repair_controller") -> GoalRun:
        """V7 明确完成修复后重置旧验收结论，开启只接收新证据的验证轮次。"""
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError("Repair requires a ticket ID")
        with self._lock:
            run = self.store.get(run_id)
            if run.status != "repairing":
                raise ValueError("Only a repairing Goal can finish a repair round")
            now = self._now()
            run.criteria = {criterion.id: CriterionRecord() for criterion in run.spec.criteria}
            run.events.append(GoalEvent(now, "verification_requested", actor, f"Fresh evidence required after repair ticket: {ticket_id}", "repairing", "verifying"))
            run.status = "verifying"
            run.updated_at = now
            return self.store.save(run)

    def fail_repair(self, run_id: str, ticket_id: str, reason: str, *, actor: str = "repair_controller") -> GoalRun:
        """修复工作流明确失败时阻止继续执行，并记录人工介入所需的原因。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Repair failure requires a reason")
        return self._transition(run_id, {"repairing"}, "blocked", "repair_failed", f"Ticket {ticket_id}: {reason[:800]}", actor)

    def exhaust(self, run_id: str, reason: str, *, actor: str = "budget_controller") -> GoalRun:
        """预算耗尽且没有正在运行的修复时将 Goal 设为 exhausted 并保存原因。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Budget exhaustion requires a reason")
        return self._transition(run_id, {"verifying", "unknown"}, "exhausted", "budget_exhausted", reason, actor)
