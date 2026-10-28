"""演示 V7.4 的安全重试、进程崩溃核实与 V5 后台任务超时恢复。"""

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from devpilot.workflow import (
    ActionRegistry, ExternalOutcome, ExternalResolverRegistry, ExternalSubmission,
    RetryPolicy, RetryableActionError, StepDefinition, UncertainActionError,
    WorkflowDefinition, WorkflowEngine, WorkflowStore,
)


class DemoClock:
    """为教程提供可以手动推进的时钟，避免测试等待真实秒数。"""

    def __init__(self):
        """设置一份固定的演示时间。"""
        self.current = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def __call__(self):
        """返回当前演示时间。"""
        return self.current

    def advance(self, seconds):
        """将演示时间前移指定秒数。"""
        self.current += timedelta(seconds=seconds)


def demo_safe_retry(directory: Path) -> None:
    """模拟一次明确可重试的临时失败，并在截止时间到达后自动完成。"""
    clock = DemoClock()
    actions = ActionRegistry()

    def flaky(ctx):
        """第一次报告临时故障，第二次返回成功。"""
        if ctx.attempt == 1:
            raise RetryableActionError("测试环境暂时繁忙")
        return {"ok": True, "attempt": ctx.attempt}

    actions.register("safe_read", flaky)
    engine = WorkflowEngine(WorkflowStore(directory / "retry"), actions, clock=clock)
    flow = WorkflowDefinition("safe-retry", "安全重试演示", (
        StepDefinition("read", action="safe_read", max_attempts=2, retry=RetryPolicy(automatic_on_transient=True, idempotent=True, delay_seconds=5)),
    ))
    run = engine.start(flow)
    print("[安全重试] 第一次：", engine.run_until_blocked(run.id).status)
    clock.advance(5)
    print("[安全重试] 到期执行：", engine.run_until_blocked(run.id).status)
    print("[安全重试] 尝试次数：", engine.store.get(run.id).steps["read"].attempts)


def demo_unknown_result(directory: Path) -> None:
    """模拟外部写入结果丢失，并由宿主核实后恢复下游步骤。"""
    actions = ActionRegistry()
    attempts = []

    def create_issue(ctx):
        """模拟 Issue 创建请求已发送但本地丢失响应。"""
        attempts.append(ctx.attempt_id)
        raise UncertainActionError("远端可能已经创建 Issue，响应未收到")

    actions.register("create_issue", create_issue)
    actions.register("report", lambda ctx: {"observed": ctx.results["issue"]["issue_id"]})
    store = WorkflowStore(directory / "unknown")
    engine = WorkflowEngine(store, actions)
    flow = WorkflowDefinition("uncertain", "未知结果演示", (
        StepDefinition("issue", action="create_issue", retry=RetryPolicy(), max_attempts=2),
        StepDefinition("report", action="report", depends_on=("issue",)),
    ))
    run = engine.start(flow)
    print("[结果未知] 首次：", engine.run_until_blocked(run.id).status)
    restarted = WorkflowEngine(WorkflowStore(directory / "unknown"), actions)
    print("[结果未知] 重启后：", restarted.recover_interrupted(run.id).status)
    print("[结果未知] 用户核实 Issue 已存在，再恢复执行")
    restarted.reconcile_step(run.id, "issue", "succeeded", actor="trusted_host", evidence="Issue #42 已存在", output={"issue_id": 42})
    print("[结果未知] 恢复结果：", restarted.run_until_blocked(run.id).status)
    print("[结果未知] 创建动作调用次数：", len(attempts))


def demo_external_timeout(directory: Path) -> None:
    """模拟 V5 后台任务超时后核实仍在运行，并继续等待原 Job。"""
    clock = DemoClock()
    actions = ActionRegistry()
    actions.register("submit_test", lambda ctx: ExternalSubmission("v5", "job-007"))
    resolver = ExternalResolverRegistry()
    resolver.register("v5", lambda reference, meta: ExternalOutcome("running"))
    engine = WorkflowEngine(WorkflowStore(directory / "background"), actions, resolver, clock=clock)
    flow = WorkflowDefinition("build", "外部任务超时演示", (
        StepDefinition("test", action="submit_test", retry=RetryPolicy(external_timeout_seconds=10)),
    ))
    run = engine.start(flow)
    print("[后台任务] 提交：", engine.run_until_blocked(run.id).status)
    clock.advance(11)
    print("[后台任务] 超时：", engine.check_external_timeouts(run.id).status)
    engine.reconcile_step(run.id, "test", "still_running", actor="trusted_host", evidence="JobStore 表示 job-007 仍在运行")
    print("[后台任务] 核实后：", engine.poll_external(run.id, "test").status)


def main() -> None:
    """按顺序演示三类恢复场景，所有状态文件保存在临时目录里。"""
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        demo_safe_retry(directory)
        demo_unknown_result(directory)
        demo_external_timeout(directory)


if __name__ == "__main__":
    main()
