"""把 V7.1～V7.5 的执行器、适配器与审批查询合成一个宿主入口。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .actions import ActionRegistry
from .adapters import WorkflowRuntimeAdapters
from .engine import WorkflowEngine
from .external import ExternalOutcome, ExternalResolverRegistry
from .model import StepContext, WorkflowDefinition, WorkflowRun
from .parallel import ParallelWorkflowRunner
from .store import WorkflowStore


@dataclass(frozen=True)
class WorkflowBindings:
    """由可信宿主注入旧版 Runtime 的实际调用入口，避免 Workflow 假装知道不同版本的函数签名。"""

    dispatch_tool: Callable[[str, dict[str, Any]], dict[str, Any]]
    create_task: Callable[[dict[str, Any]], str]
    run_assignment: Callable[[str, str], dict[str, Any]]
    submit_job: Callable[[str, dict[str, Any]], str]
    query_job: Callable[[str], ExternalOutcome]
    merge_worktrees: Callable[[StepContext], dict[str, Any]]
    allowed_tools: frozenset[str]
    allowed_mcp_tools: frozenset[str]

    def __post_init__(self) -> None:
        """检查每个旧版入口都由宿主显式提供，并拒绝无工具名单的配置。"""
        callbacks = (self.dispatch_tool, self.create_task, self.run_assignment,
                     self.submit_job, self.query_job, self.merge_worktrees)
        if not all(callable(item) for item in callbacks):
            raise TypeError("All WorkflowBindings callbacks must be callable")
        if not self.allowed_tools or not self.allowed_mcp_tools:
            raise ValueError("Explicit nonempty V1 and MCP tool allowlists are required")
        if any(not isinstance(item, str) or not item.startswith("mcp__") for item in self.allowed_mcp_tools):
            raise ValueError("MCP tools must use the mcp__ namespace")


@dataclass(frozen=True)
class ApprovalInboxItem:
    """向可信审批界面展示一次待处理审批所需的最小信息。"""

    run_id: str
    workflow_id: str
    step_id: str
    request_id: str
    expires_at: str | None
    purpose: str
    approvers: tuple[str, ...]


class WorkflowApplication:
    """统一启动、推进、审批、外部轮询和关闭工作流，但不绕过各旧 Runtime 自身的权限与所有权规则。"""

    def __init__(self, state_dir: Path, bindings: WorkflowBindings, *, max_workers: int = 3):
        """创建持久化工作流和并行调度器，并将可信宿主提供的 V1～V6 接口注册为动作。"""
        self.store = WorkflowStore(Path(state_dir))
        self.actions = ActionRegistry()
        self.external = ExternalResolverRegistry()
        self.adapters = WorkflowRuntimeAdapters(self.actions, self.external)
        self.adapters.register_v1_tool(bindings.dispatch_tool, allowed_tools=bindings.allowed_tools)
        self.adapters.register_v3_task(bindings.create_task)
        self.adapters.register_v4_assignment(bindings.run_assignment)
        self.adapters.register_v5_job(bindings.submit_job, bindings.query_job)
        self.adapters.register_v6_mcp(bindings.dispatch_tool, allowed_tools=bindings.allowed_mcp_tools)
        self.actions.register("coding.merge", bindings.merge_worktrees)
        self.engine = WorkflowEngine(self.store, self.actions, self.external)
        self.runner = ParallelWorkflowRunner(self.engine, max_workers=max_workers)

    def __enter__(self) -> WorkflowApplication:
        """允许宿主通过 with 管理 WorkflowApplication 的执行生命周期。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """离开 with 时等待已启动的步骤落盘并关闭线程池。"""
        self.close()
        return False

    def start(self, definition: WorkflowDefinition, inputs: dict[str, Any] | None = None) -> WorkflowRun:
        """验证 Workflow 蓝图并创建一条独立的持久化运行记录。"""
        return self.engine.start(definition, inputs)

    def tick(self, run_id: str, *, poll_external: bool = False) -> WorkflowRun:
        """按需查询外部 Job、检查审批过期，并调度本轮可执行步骤。"""
        if poll_external:
            run = self.store.get(run_id)
            for step_id, state in run.steps.items():
                if state.status == "waiting_external":
                    self.engine.poll_external(run_id, step_id)
            self.engine.check_external_timeouts(run_id)
        self.engine.expire_approvals(run_id)
        return self.runner.tick(run_id)

    def run_until_blocked(self, run_id: str, *, timeout_seconds: float = 10) -> WorkflowRun:
        """持续收集本进程并行步骤直到完成或等待外部作业、人工审批等状态。"""
        return self.runner.run_until_blocked(run_id, timeout_seconds=timeout_seconds)

    def pending_approvals(self, run_id: str | None = None) -> tuple[ApprovalInboxItem, ...]:
        """读取磁盘中尚未处理的审批请求，供可信 UI 或审批线程展示。"""
        runs = [self.store.get(run_id)] if run_id is not None else self.store.list_runs()
        result: list[ApprovalInboxItem] = []
        for run in runs:
            for step in run.definition.steps:
                state = run.steps[step.id]
                request = state.approval_request
                if state.status != "waiting_approval" or request is None:
                    continue
                policy = step.approval
                result.append(ApprovalInboxItem(run.id, run.definition.id, step.id,
                                                request.request_id, request.expires_at,
                                                policy.purpose if policy else "",
                                                policy.approvers if policy else ()))
        return tuple(result)

    def approve(self, run_id: str, step_id: str, *, actor: str, request_id: str, approved: bool, note: str = "") -> WorkflowRun:
        """让已验证身份的宿主提交带请求编号的审批决定，并保留审计记录。"""
        return self.engine.approve_step(run_id, step_id, approved, note, actor=actor, request_id=request_id)

    def recover(self, run_id: str) -> WorkflowRun:
        """仅在该工作流没有本进程活动线程时识别重启遗留步骤，不自动重放副作用。"""
        if self.runner.active_steps(run_id):
            raise RuntimeError("Cannot recover while this application's steps are still running")
        return self.engine.recover_interrupted(run_id)

    def close(self) -> None:
        """停止调度、等待正在执行的动作完成并保存它们的最新状态。"""
        self.runner.close()
