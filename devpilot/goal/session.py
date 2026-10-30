"""将 Goal 验收、受控修复、V7 审批与恢复边界串成可逐步推进的会话。"""

from dataclasses import dataclass
from threading import RLock
from typing import Literal

from .application import GoalApplication
from .budget import BudgetExceededError

SessionAction = Literal[
    "need_initial_workflow", "collected", "repair_prepared", "repair_dispatched",
    "repair_progress", "waiting_approval", "waiting_external", "needs_recovery",
    "needs_reconciliation", "verification_unknown", "terminal", "no_progress",
]


@dataclass(frozen=True)
class SessionUpdate:
    """向 CLI/UI 返回本次真正推进了什么，以及当前还在等待什么。"""

    goal_run_id: str
    goal_status: str
    action: SessionAction
    detail: str
    ticket_id: str | None = None
    workflow_run_id: str | None = None
    workflow_status: str | None = None


class GoalSession:
    """通过持久化状态每次推进一小步，而不在异常情况下自行重放副作用。"""

    def __init__(self, app: GoalApplication) -> None:
        """使用 V8.5 已有统一入口，并对单个会话实例内的推进加锁。"""
        self.app = app
        self._lock = RLock()

    @staticmethod
    def _latest_verification_event(run) -> str | None:
        """识别最近一次真正的验收操作，而不是把普通备注误当成新一轮验证。"""
        kinds = {"verification_requested", "verification_resumed", "verification_checked", "goal_verified"}
        return next((event.event_type for event in reversed(run.events) if event.event_type in kinds), None)

    def _result(self, run_id: str, action: SessionAction, detail: str, ticket=None, workflow_status: str | None = None) -> SessionUpdate:
        """从持久化存储重新读取真实状态，再生成供宿主展示的单步结果。"""
        goal = self.app.get(run_id)
        return SessionUpdate(run_id, goal.status, action, detail,
                             getattr(ticket, "id", None), getattr(ticket, "workflow_run_id", None), workflow_status)

    def advance(self, run_id: str) -> SessionUpdate:
        """根据最新 Goal 和修复单状态决定本轮唯一动作，遇到未知或审批时停止。"""
        with self._lock:
            goal = self.app.get(run_id)
            if goal.status in {"verified", "exhausted", "blocked"}:
                return self._result(run_id, "terminal", f"Goal 已结束：{goal.status}")
            if goal.status == "unknown":
                return self._result(run_id, "verification_unknown", "证据不足，请先补充证据并由宿主恢复验证")
            if goal.status in {"pending", "executing"}:
                return self._result(run_id, "need_initial_workflow", "等待宿主实际执行初始开发流程，不能假装已经修复")

            tickets = self.app.repair_store.list_for_goal(run_id)
            active = next((item for item in reversed(tickets)
                           if item.status in {"prepared", "dispatching", "running"}), None)
            if active is not None:
                if active.status == "prepared":
                    try:
                        ticket = self.app.dispatch_repair(run_id, active.id)
                    except BudgetExceededError as exc:
                        return self._result(run_id, "terminal", str(exc), active)
                    return self._result(run_id, "repair_dispatched", "修复单已关联 V7 Workflow", ticket)
                if active.status == "dispatching":
                    return self._result(run_id, "needs_reconciliation", "Workflow 启动结果未知，必须先查询 V7 Store 对账", active)
                workflow = self.app.workflow.store.get(active.workflow_run_id)
                if any(step.status == "running" for step in workflow.steps.values()) and not self.app.workflow.runner.active_steps(workflow.id):
                    return self._result(run_id, "needs_recovery", "发现无活动线程的执行中步骤，必须先核实副作用", active, workflow.status)
                if workflow.status in {"waiting_approval", "waiting_external", "recovery_required"}:
                    action = "waiting_approval" if workflow.status == "waiting_approval" else (
                        "waiting_external" if workflow.status == "waiting_external" else "needs_recovery")
                    return self._result(run_id, action, f"V7 工作流当前状态：{workflow.status}", active, workflow.status)
                ticket = self.app.advance_repair(run_id, active.id)
                workflow = self.app.workflow.store.get(active.workflow_run_id)
                if workflow.status in {"waiting_approval", "waiting_external", "recovery_required"}:
                    action = "waiting_approval" if workflow.status == "waiting_approval" else (
                        "waiting_external" if workflow.status == "waiting_external" else "needs_recovery")
                    return self._result(run_id, action, f"V7 工作流等待：{workflow.status}", ticket, workflow.status)
                return self._result(run_id, "repair_progress", f"修复单状态：{ticket.status}", ticket, workflow.status)

            if goal.status == "repairing":
                if tickets and tickets[-1].status == "completed":
                    self.app.poll_repair(run_id, tickets[-1].id)
                    return self._result(run_id, "repair_progress", "修复已完成，重新开启验收", tickets[-1])
                return self._result(run_id, "needs_reconciliation", "修复中却没有有效修复单，需人工核实")
            if goal.status != "verifying":
                return self._result(run_id, "no_progress", f"当前状态不允许推进：{goal.status}")

            if self._latest_verification_event(goal) in {"verification_requested", "verification_resumed"}:
                report = self.app.collect_and_verify(run_id)
                return self._result(run_id, "collected", f"最新验收状态：{report.status}")
            if self._latest_verification_event(goal) != "verification_checked":
                return self._result(run_id, "no_progress", "没有可用于修复的新鲜验收失败记录")
            if tickets and tickets[-1].status == "failed":
                return self._result(run_id, "needs_reconciliation", "上一份修复单失败，宿主需决定是否重新准备")
            try:
                ticket = self.app.prepare_repair(run_id)
            except BudgetExceededError as exc:
                return self._result(run_id, "terminal", str(exc))
            return self._result(run_id, "repair_prepared", "验收失败，已保存修复意图但尚未执行", ticket)

    def run_until_boundary(self, run_id: str, *, max_steps: int = 20) -> tuple[SessionUpdate, ...]:
        """在最多指定推进次数内运行到审批、外部等待、未知、成功或预算耗尽为止。"""
        if type(max_steps) is not int or not 1 <= max_steps <= 1000:
            raise ValueError("max_steps must be between 1 and 1000")
        updates = []
        terminal = {"terminal", "waiting_approval", "waiting_external", "needs_recovery", "needs_reconciliation",
                    "verification_unknown", "need_initial_workflow", "no_progress"}
        for _ in range(max_steps):
            update = self.advance(run_id)
            updates.append(update)
            if update.action in terminal:
                break
        return tuple(updates)
