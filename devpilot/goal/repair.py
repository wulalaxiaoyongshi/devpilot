"""把验收失败转成一次受控 V7 Workflow 修复，并管理状态衔接与故障恢复。"""

from collections.abc import Callable
from threading import RLock
from typing import Any

from .feedback import FeedbackGenerator, RepairFeedback
from .repair_store import RepairStore, RepairTicket
from .runtime import GoalRuntime
from .budget import GoalBudgetManager, BudgetExceededError


class RepairController:
    """先持久化修复意图再启动 V7，并在修复完成后要求重新采集验收证据。"""

    def __init__(
        self, goals: GoalRuntime, repairs: RepairStore, workflow: Any,
        plan: Callable[[RepairFeedback], Any], *, max_rounds: int = 3, budget: GoalBudgetManager | None = None,
    ) -> None:
        """接入 Goal、修复单存储器、V7 应用和可信 Workflow 构造函数。"""
        if type(max_rounds) is not int or max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.goals = goals
        self.repairs = repairs
        self.workflow = workflow
        self.plan = plan
        self.max_rounds = max_rounds
        self.budget = budget
        self.feedback = FeedbackGenerator(goals.store)
        self._lock = RLock()

    def _require_budget(self, goal_run_id: str, used_rounds: int, *, pending_ticket: RepairTicket | None = None) -> None:
        """检查真实预算并停止尚未启动的修复，防止预算耗尽后提交新 Workflow。"""
        if self.budget is None:
            return
        decision = self.budget.check(goal_run_id, used_rounds=used_rounds)
        if decision.allowed:
            return
        reason = "Budget exhausted: " + ", ".join(decision.reasons)
        if pending_ticket is not None:
            self.repairs.update(pending_ticket, status="failed", error=reason)
        goal = self.goals.get(goal_run_id)
        if goal.status in {"verifying", "unknown"}:
            self.goals.exhaust(goal_run_id, reason)
        raise BudgetExceededError(reason)

    def prepare(self, goal_run_id: str) -> RepairTicket:
        """确认存在明确失败并且没有未结束修复单后，保存一份新的修复请求。"""
        with self._lock:
            existing = self.repairs.list_for_goal(goal_run_id)
            if any(item.status in {"prepared", "dispatching", "running"} for item in existing):
                raise ValueError("There is already an active repair ticket")
            data = self.feedback.build(goal_run_id)
            self._require_budget(goal_run_id, len(existing))
            if len(existing) >= self.max_rounds:
                raise ValueError("Repair round limit reached")
            return self.repairs.create(goal_run_id, len(existing) + 1, data)

    def dispatch(self, goal_run_id: str, ticket_id: str) -> RepairTicket:
        """先记录不可自动重放的启动意图，再把可信 Workflow 提交给 V7。"""
        with self._lock:
            ticket = self.repairs.get(goal_run_id, ticket_id)
            if ticket.status != "prepared":
                raise ValueError("Only prepared repairs may be dispatched")
            goal = self.goals.get(goal_run_id)
            if goal.status != "verifying" or goal.version != ticket.feedback.goal_version:
                raise ValueError("Goal changed since repair was prepared; recheck verification")
            self._require_budget(goal_run_id, ticket.round_number - 1, pending_ticket=ticket)
            definition = self.plan(ticket.feedback)
            if definition is None:
                raise ValueError("Trusted workflow plan must not be empty")
            ticket = self.repairs.update(ticket, status="dispatching")
            inputs = ticket.feedback.as_context()
            inputs["repair_ticket_id"] = ticket.id
            inputs["repair_round"] = ticket.round_number
            run = self.workflow.start(definition, inputs)
            return self.link_dispatched(goal_run_id, ticket_id, run.id)

    def link_dispatched(self, goal_run_id: str, ticket_id: str, workflow_run_id: str) -> RepairTicket:
        """核对已有 Workflow 的修复单编号后，将不确定的启动状态恢复为已连接状态。"""
        with self._lock:
            ticket = self.repairs.get(goal_run_id, ticket_id)
            if ticket.status not in {"dispatching", "running"}:
                raise ValueError("Only dispatching or running repair can be linked")
            run = self.workflow.store.get(workflow_run_id)
            if run.inputs.get("repair_ticket_id") != ticket.id or run.inputs.get("goal_run_id") != goal_run_id:
                raise ValueError("Workflow does not belong to the repair ticket")
            if ticket.workflow_run_id is not None and ticket.workflow_run_id != workflow_run_id:
                raise ValueError("Repair already linked to another Workflow")
            if ticket.status == "dispatching":
                ticket = self.repairs.update(ticket, status="running", workflow_run_id=workflow_run_id)
            goal = self.goals.get(goal_run_id)
            if goal.status == "verifying":
                self.goals.begin_repair(goal_run_id, ticket_id)
            elif goal.status != "repairing":
                raise ValueError("Goal cannot be linked while in its current status")
            return ticket

    def abandon_dispatch(self, goal_run_id: str, ticket_id: str, reason: str) -> RepairTicket:
        """可信宿主确认未创建对应 Workflow 后，显式放弃悬而未决的启动请求。"""
        if not reason.strip():
            raise ValueError("A human reconciliation reason is required")
        with self._lock:
            ticket = self.repairs.get(goal_run_id, ticket_id)
            if ticket.status != "dispatching":
                raise ValueError("Only an unresolved dispatch can be abandoned")
            return self.repairs.update(ticket, status="failed", error=f"Host verified no workflow was started: {reason}")

    def poll(self, goal_run_id: str, ticket_id: str) -> RepairTicket:
        """读取 V7 的真实执行状态，仅在确定成功后开启新一轮验收。"""
        with self._lock:
            ticket = self.repairs.get(goal_run_id, ticket_id)
            if ticket.status == "dispatching":
                raise RuntimeError("Dispatch outcome unknown; first reconcile with V7 WorkflowStore")
            if ticket.status not in {"running", "completed"}:
                raise ValueError("Repair is not running or completed")
            if ticket.status == "completed":
                if self.goals.get(goal_run_id).status == "repairing":
                    self.goals.finish_repair(goal_run_id, ticket_id)
                return ticket
            if self.goals.get(goal_run_id).status == "verifying":
                self.goals.begin_repair(goal_run_id, ticket_id)
            run = self.workflow.store.get(ticket.workflow_run_id)
            if run.status == "completed":
                ticket = self.repairs.update(ticket, status="completed")
                self.goals.finish_repair(goal_run_id, ticket_id)
            elif run.status in {"failed", "rejected", "approval_expired", "recovery_required"}:
                ticket = self.repairs.update(ticket, status="failed", error=f"Workflow ended in {run.status}")
                self.goals.fail_repair(goal_run_id, ticket_id, f"Workflow ended in {run.status}")
            return ticket
