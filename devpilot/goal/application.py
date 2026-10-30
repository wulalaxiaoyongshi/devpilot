"""把 V8.1～V8.5 目标、证据、验收、修复及预算组合成统一宿主入口。"""

from pathlib import Path
from typing import Any, Callable

from .budget import BudgetDecision, BudgetStore, GoalBudget, GoalBudgetManager
from .collector import EvidenceCollector
from .evidence_store import EvidenceStore
from .model import GoalRun, GoalSpec
from .repair import RepairController
from .repair_store import RepairStore, RepairTicket
from .runtime import GoalRuntime
from .store import GoalStore
from .verifier import GoalVerificationReport, GoalVerifier


class GoalApplication:
    """统一管理 Goal 的创建、实证验证、预算计费和受控 V7 修复。"""

    def __init__(
        self, state_dir: str | Path, workspace: str | Path, workflow: Any,
        plan: Callable[..., Any], *, allowed_commands: tuple[tuple[str, ...], ...] = (),
        clock: Callable | None = None,
    ) -> None:
        """复用各版本组件并注入可信 V7 入口，不在应用层绕过工具权限。"""
        root = Path(state_dir)
        self.goal_store = GoalStore(root / "goals")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.repair_store = RepairStore(root / "repairs")
        self.budget_store = BudgetStore(root / "budgets")
        self.goals = GoalRuntime(self.goal_store, clock=clock)
        self.budgets = GoalBudgetManager(self.budget_store, clock=clock)
        self.collector = EvidenceCollector(self.goal_store, self.evidence_store, workspace, allowed_commands=allowed_commands, clock=clock)
        self.verifier = GoalVerifier(self.goal_store, self.evidence_store, workspace, clock=clock)
        self.repairs = RepairController(self.goals, self.repair_store, workflow, plan, budget=self.budgets)
        self.workflow = workflow

    def start(self, spec: GoalSpec, policy: GoalBudget, inputs: dict | None = None) -> GoalRun:
        """创建 Goal 后立即登记不可自动扩容的预算，出错时不允许派发未登记目标。"""
        goal = self.goals.start(spec, inputs)
        self.budgets.open(goal.id, policy)
        return goal

    def begin_execution(self, goal_run_id: str) -> GoalRun:
        """将已经开始执行开发任务的 Goal 标记为 executing。"""
        self.budget_store.get(goal_run_id)
        return self.goals.begin_execution(goal_run_id)

    def begin_verification(self, goal_run_id: str) -> GoalRun:
        """在外部开发流程完成后让 Goal 进入等待真实证据的验证状态。"""
        self.budget_store.get(goal_run_id)
        return self.goals.begin_verification(goal_run_id)

    def collect_and_verify(self, goal_run_id: str) -> GoalVerificationReport:
        """重新采集当前代码的事实证据，再交给 V8.3 产生独立验收结果。"""
        if self.goals.get(goal_run_id).status != "verifying":
            raise ValueError("Only verifying goals may collect and verify")
        self.budget_store.get(goal_run_id)
        self.collector.collect_all(goal_run_id)
        return self.verifier.verify(goal_run_id)

    def prepare_repair(self, goal_run_id: str) -> RepairTicket:
        """按失败验收和最新预算生成一份不会自动执行的持久化修复单。"""
        return self.repairs.prepare(goal_run_id)

    def dispatch_repair(self, goal_run_id: str, ticket_id: str) -> RepairTicket:
        """再次检查预算后调用可信 V7 入口提交修复工作流。"""
        return self.repairs.dispatch(goal_run_id, ticket_id)

    def poll_repair(self, goal_run_id: str, ticket_id: str) -> RepairTicket:
        """查看 V7 Workflow 的结果并在确定完成后开启新一轮验收。"""
        return self.repairs.poll(goal_run_id, ticket_id)

    def advance_repair(self, goal_run_id: str, ticket_id: str) -> RepairTicket:
        """驱动已关联的 V7 修复直到阻塞，再同步最新执行结果到修复单。"""
        ticket = self.repair_store.get(goal_run_id, ticket_id)
        if ticket.status != "running" or ticket.workflow_run_id is None:
            raise ValueError("Only running repairs may be advanced")
        self.workflow.run_until_blocked(ticket.workflow_run_id)
        return self.repairs.poll(goal_run_id, ticket_id)

    def record_usage(self, goal_run_id: str, event_id: str, *, tokens: int = 0, cost_usd: float = 0.0, source: str = "host") -> None:
        """接收可信宿主的用量事件，重复上报相同事件不会再次扣费。"""
        self.budgets.record_usage(goal_run_id, event_id, tokens=tokens, cost_usd=cost_usd, source=source)

    def budget_status(self, goal_run_id: str) -> BudgetDecision:
        """根据已保存修复轮次和实际用量展示当前预算剩余额度。"""
        rounds = len(self.repair_store.list_for_goal(goal_run_id))
        return self.budgets.check(goal_run_id, used_rounds=rounds)

    def get(self, goal_run_id: str) -> GoalRun:
        """向 UI 和其他模块返回最新的目标状态及验收记录。"""
        return self.goals.get(goal_run_id)
