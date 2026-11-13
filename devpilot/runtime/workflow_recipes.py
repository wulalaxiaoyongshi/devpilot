"""V9.8：将分析、代码修改、Issue 修复、PR 发布与失败返工组合为可校验的 V7 工作流。"""

from __future__ import annotations

from typing import Any

from devpilot.workflow.approval import ApprovalPolicy
from devpilot.workflow.engine import WorkflowEngine, validate_definition
from devpilot.workflow.model import StepDefinition, WorkflowDefinition
from devpilot.workflow.parallel import ParallelWorkflowRunner


def analysis_workflow(workflow_id: str) -> WorkflowDefinition:
    """为纯分析任务创建只读报告步骤，不创建 Worker 或写入仓库。"""
    result = WorkflowDefinition(workflow_id, "只读代码分析", (
        StepDefinition(id="analysis_report", action="general.analysis_report"),
    ))
    validate_definition(result)
    return result


def with_remote_delivery(definition: WorkflowDefinition, *, approver: str) -> WorkflowDefinition:
    """在已验收的本地交付报告之后追加发布审批与远程 PR 创建步骤。"""
    if not any(step.id == "report" for step in definition.steps):
        raise ValueError("PR delivery needs a local verification report")
    steps = definition.steps + (
        StepDefinition(id="publish_review", kind="approval", depends_on=("report",),
                       approval=ApprovalPolicy(approvers=(approver,), requester="lead",
                                               purpose="允许推送指定修复分支并创建 GitHub PR；禁止合并 main")),
        StepDefinition(id="publish_pr", action="general.publish_pr", depends_on=("publish_review",),
                       exclusive_resources=("github:publish",)),
    )
    result = WorkflowDefinition(definition.id, "GitHub Issue 修复及 PR 交付", steps)
    validate_definition(result)
    return result


def repair_workflow(definition: WorkflowDefinition) -> WorkflowDefinition:
    """将已审核的动态开发计划裁剪成独立的返工 Workflow，由 V8 在结束后重新验收。"""
    steps = tuple(step for step in definition.steps if step.id == "preapproval" or
                  step.action in {"general.worker", "general.merge"})
    result = WorkflowDefinition(definition.id, "V8 失败反馈触发的受控返工", steps)
    validate_definition(result)
    return result


def repair_delivery_workflow(workflow_id: str, *, approver: str, publish: bool) -> WorkflowDefinition:
    """在返工重新验证通过后创建单独的人工交付审批，按需附加 GitHub PR。"""
    base = WorkflowDefinition(workflow_id, "返工后的验收交付", (
        StepDefinition(id="delivery_review", kind="approval",
                       approval=ApprovalPolicy(approvers=(approver,), requester="lead",
                                               purpose="审查修复后的 Git 变更和最新 V8 证据")),
        StepDefinition(id="report", action="general.report", depends_on=("delivery_review",)),
    ))
    validate_definition(base)
    return with_remote_delivery(base, approver=approver) if publish else base


class RepairWorkflowAdapter:
    """将现有 V7 Engine 与并行 Runner 适配成 V8 RepairController 所需的窄接口。"""

    def __init__(self, engine: WorkflowEngine, runner: ParallelWorkflowRunner):
        """绑定同一套 V7 持久化状态和执行线程池。"""
        self.engine = engine
        self.runner = runner
        self.store = engine.store

    def start(self, definition: WorkflowDefinition, inputs: dict[str, Any]):
        """仅创建一条真实 V7 Workflow，不在审批完成前运行写入步骤。"""
        return self.engine.start(definition, inputs)

    def run_until_blocked(self, run_id: str):
        """由原 V7 并行 Runner 将返工步骤推进到阻塞、完成或失败。"""
        return self.runner.run_until_blocked(run_id)
