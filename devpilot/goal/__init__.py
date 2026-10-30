"""V8.1 目标定义、校验、持久化与运行入口。"""

from .application import GoalApplication
from .budget import (BudgetAccount, BudgetConflictError, BudgetDecision, BudgetExceededError, BudgetStore,
                     GoalBudget, GoalBudgetManager, UsageEvent)
from .collector import EvidenceCollector
from .evidence import EvidenceRecord
from .evidence_store import EvidenceStore
from .model import AcceptanceCriterion, CriterionRecord, GoalEvent, GoalRun, GoalSpec
from .runtime import GoalRuntime
from .store import GoalConflictError, GoalStore
from .verifier import CriterionVerdict, GoalVerificationReport, GoalVerifier
from .feedback import FeedbackGenerator, FeedbackItem, RepairFeedback
from .repair import RepairController
from .repair_store import RepairStore, RepairTicket, RepairConflictError
from .validation import validate_spec
from .session import GoalSession, SessionUpdate
from .delivery import GoalDeliveryBuilder, GoalDeliveryReport, GoalReportStore, DeliveryCriterion, DeliveryRepair

__all__ = [
    "GoalApplication", "GoalBudget", "GoalBudgetManager", "BudgetAccount", "UsageEvent",
    "BudgetStore", "BudgetDecision", "BudgetExceededError", "BudgetConflictError",
    "EvidenceCollector", "EvidenceRecord", "EvidenceStore",
    "AcceptanceCriterion", "CriterionRecord", "GoalEvent", "GoalRun", "GoalSpec",
    "GoalRuntime", "GoalStore", "GoalConflictError", "validate_spec",
    "CriterionVerdict", "GoalVerificationReport", "GoalVerifier",
    "FeedbackGenerator", "FeedbackItem", "RepairFeedback", "RepairController",
    "RepairStore", "RepairTicket", "RepairConflictError",
    "GoalSession", "SessionUpdate", "GoalDeliveryBuilder", "GoalDeliveryReport",
    "GoalReportStore", "DeliveryCriterion", "DeliveryRepair",
]
