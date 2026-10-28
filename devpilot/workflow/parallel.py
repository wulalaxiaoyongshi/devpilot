"""为 V7.4 WorkflowEngine 增加有并发上限、资源互斥和持久化汇合的线程调度层。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic, sleep
from threading import RLock
from typing import Any
from uuid import uuid4

from .engine import WorkflowEngine
from .external import ExternalSubmission
from .model import StepContext, StepDefinition, WorkflowRun
from .recovery import RetryableActionError, UncertainActionError
from .store import json_snapshot


@dataclass(frozen=True)
class _Flight:
    """记录一次运行中的步骤、尝试编号和它持有的独占资源。"""

    run_id: str
    step_id: str
    attempt_id: str
    resources: tuple[str, ...]


class ParallelWorkflowRunner:
    """用线程池并行执行互不依赖的 Workflow 动作，并由调度线程串行更新 WorkflowStore。"""

    def __init__(self, engine: WorkflowEngine, *, max_workers: int = 4):
        """接入已有 WorkflowEngine 并设置整个 Runner 同时运行的动作数上限。"""
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self.engine = engine
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workflow-step")
        self._flights: dict[Future[Any], _Flight] = {}
        self._lock = RLock()
        self._closed = False

    def __enter__(self) -> ParallelWorkflowRunner:
        """让调度器能够在 with 代码块内使用并在退出时自动等待并关闭线程。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """离开 with 时完成正在执行的动作、保存结果并释放线程池。"""
        self.close()
        return False

    def active_steps(self, run_id: str | None = None) -> tuple[tuple[str, str], ...]:
        """返回仍在运行的 Workflow 和 Step 编号，用于查看当前并行任务。"""
        with self._lock:
            return tuple(sorted((job.run_id, job.step_id) for job in self._flights.values() if run_id is None or job.run_id == run_id))

    def _claimed_resources(self) -> set[str]:
        """汇总运行中、外部等待和结果未知步骤的持久化资源预约，避免异步 Job 提交后提前放锁。"""
        claimed = {resource for flight in self._flights.values() for resource in flight.resources}
        for run in self.engine.store.list_runs():
            for step in run.definition.steps:
                if run.steps[step.id].status in {"running", "waiting_external", "recovery_required"}:
                    claimed.update(step.exclusive_resources)
        return claimed

    def _make_context(self, run: WorkflowRun, step: StepDefinition, attempt_id: str) -> StepContext:
        """按 V7.4 的格式生成包含已完成输出和稳定幂等键的步骤执行上下文。"""
        return StepContext(
            run_id=run.id, workflow_id=run.definition.id, step_id=step.id,
            attempt=run.steps[step.id].attempts,
            inputs=json_snapshot(run.inputs), params=json_snapshot(step.params),
            results=json_snapshot({name: item.output for name, item in run.steps.items() if item.status == "succeeded"}),
            attempt_id=attempt_id, idempotency_key=f"{run.id}:{step.id}",
        )

    def _finish_ready(self) -> None:
        """收集已经结束的线程结果，并按照 V7.4 规则串行保存成功、失败和恢复状态。"""
        for future, flight in list(self._flights.items()):
            if not future.done():
                continue
            with self.engine._lock:
                run = self.engine.store.get(flight.run_id)
                state = run.steps[flight.step_id]
                if state.status != "running" or state.attempt_id != flight.attempt_id:
                    self._flights.pop(future)
                    continue
                step = next(item for item in run.definition.steps if item.id == flight.step_id)
                try:
                    output = future.result()
                    if isinstance(output, ExternalSubmission):
                        state.output = {
                            "provider": output.provider, "reference": output.reference,
                            "metadata": json_snapshot(output.metadata),
                        }
                        state.status = "waiting_external"
                        state.external_submitted_at = self.engine._approval_now().isoformat()
                    else:
                        result = json_snapshot(output)
                        if not isinstance(result, dict):
                            raise TypeError("Workflow action must return a JSON object")
                        state.output = result
                        state.status = "succeeded"
                except UncertainActionError as error:
                    self.engine._needs_reconciliation(state, f"UncertainActionError: {error}")
                except RetryableActionError as error:
                    if not self.engine._schedule_safe_retry(state, step, error):
                        state.status = "failed"
                        state.error = f"RetryableActionError: {error}"
                except Exception as error:
                    state.status = "failed"
                    state.error = f"{type(error).__name__}: {error}"
                self.engine._save(run)
                self._flights.pop(future)

    def _schedule(self, run_id: str) -> None:
        """按依赖与条件挑选可执行步骤，忽略资源冲突的分支并提交其余独立动作。"""
        with self.engine._lock:
            run = self.engine.store.get(run_id)
            if run.status in {"failed", "recovery_required", "rejected", "approval_expired", "completed"}:
                return
            self.engine._wake_due_retries(run)
            run = self.engine.store.get(run_id)
            changed = True
            while changed:
                changed = False
                if run.status in {"failed", "recovery_required", "rejected", "approval_expired", "completed"}:
                    break
                for step in run.definition.steps:
                    state = run.steps[step.id]
                    if state.status != "pending":
                        continue
                    skipped_required = any(
                        run.steps[parent].status == "skipped" and parent not in step.allow_skipped_dependencies
                        for parent in step.depends_on
                    )
                    ready = all(
                        run.steps[parent].status == "succeeded"
                        or (run.steps[parent].status == "skipped" and parent in step.allow_skipped_dependencies)
                        for parent in step.depends_on
                    )
                    if skipped_required or (ready and not self.engine._matches_condition(run, step)):
                        state.status = "skipped"
                        state.note = "Dependency was skipped or condition did not match"
                        self.engine._save(run)
                        changed = True
                        break
                    if not ready:
                        continue
                    if step.kind == "approval":
                        self.engine._open_approval(run, step)
                        changed = True
                        break
                    if len(self._flights) >= self.max_workers:
                        continue
                    if not self._claimed_resources().isdisjoint(step.exclusive_resources):
                        continue
                    try:
                        handler = self.engine.actions.get(step.action)
                    except Exception as error:
                        state.status = "failed"
                        state.error = f"{type(error).__name__}: {error}"
                        self.engine._save(run)
                        return
                    state.status = "running"
                    state.attempts += 1
                    state.attempt_id = uuid4().hex
                    state.error = None
                    state.retry_at = None
                    ctx = self._make_context(run, step, state.attempt_id)
                    self.engine._save(run)
                    try:
                        future = self._pool.submit(handler, ctx)
                    except Exception as error:
                        state.status = "failed"
                        state.error = f"{type(error).__name__}: {error}"
                        self.engine._save(run)
                        return
                    self._flights[future] = _Flight(run.id, step.id, state.attempt_id, step.exclusive_resources)
                    changed = True
                    break
                if changed:
                    run = self.engine.store.get(run_id)

    def tick(self, run_id: str) -> WorkflowRun:
        """回收已经完成的步骤并启动当前可并行的步骤，然后立即返回磁盘中的最新状态。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("ParallelWorkflowRunner is closed")
            self._finish_ready()
            self._schedule(run_id)
            return self.engine.store.get(run_id)

    def run_until_blocked(self, run_id: str, *, timeout_seconds: float = 30, poll_interval: float = 0.01) -> WorkflowRun:
        """持续收集并行结果直至本轮无可推进的工作或超时，不自动越过审批和外部 Job。"""
        if timeout_seconds < 0 or poll_interval <= 0:
            raise ValueError("timeout_seconds must be non-negative and poll_interval must be positive")
        deadline = monotonic() + timeout_seconds
        while True:
            run = self.tick(run_id)
            if not self.active_steps(run_id):
                if not any(flight for flight in self._flights.values()):
                    return run
                if run.status in {"completed", "failed", "rejected", "approval_expired", "recovery_required"}:
                    return run
                if run.status in {"waiting_approval", "waiting_external", "retry_wait"}:
                    return run
            if monotonic() >= deadline:
                return run
            sleep(min(poll_interval, max(0.0, deadline - monotonic())))

    def close(self) -> None:
        """停止新调度并等待所有已运行的动作结束，再将它们的结果保存到 WorkflowStore。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._finish_ready()
