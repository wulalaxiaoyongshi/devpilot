"""使用真实 Workflow Engine 演示审批申请、可信审批、重启恢复和过期重发。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from devpilot.workflow import ActionRegistry, ApprovalPolicy, StepDefinition, WorkflowDefinition, WorkflowEngine, WorkflowStore


class DemoClock:
    """提供可手动推进的时间，帮助演示无需等待的审批过期。"""

    def __init__(self):
        """从固定 UTC 时间启动演示。"""
        self.now = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)

    def __call__(self):
        """返回演示的当前时间。"""
        return self.now

    def advance(self, seconds: int):
        """人为推进时钟以触发审批超时。"""
        self.now += timedelta(seconds=seconds)


def make_engine(directory: Path, clock: DemoClock, actions: ActionRegistry) -> WorkflowEngine:
    """使用同一个工作流存储目录构造一个可重启的 Engine。"""
    return WorkflowEngine(WorkflowStore(directory), actions, clock=clock)


def make_flow() -> WorkflowDefinition:
    """创建测试成功后必须经 reviewer 审批才能发布的工作流。"""
    return WorkflowDefinition("release-flow", "代码发布审批", (
        StepDefinition("test", action="run_tests"),
        StepDefinition("review", kind="approval", depends_on=("test",), approval=ApprovalPolicy(
            approvers=("reviewer",), timeout_seconds=60, requester="agent",
            purpose="确认代码测试通过后才能发布",
        )),
        StepDefinition("release", action="release_code", depends_on=("review",)),
    ))


def main() -> None:
    """展示申请、重启、成功审批以及另一次审批超时后重新发单的过程。"""
    with TemporaryDirectory() as folder:
        clock = DemoClock()
        directory = Path(folder) / "runs"
        actions = ActionRegistry()
        actions.register("run_tests", lambda ctx: {"pytest": "passed"})
        actions.register("release_code", lambda ctx: {"release": "published"})
        engine = make_engine(directory, clock, actions)
        first = engine.start(make_flow())
        waiting = engine.run_until_blocked(first.id)
        request = engine.get_approval_request(first.id, "review")
        print("[审批申请]", waiting.status, request.request_id)
        print("[截止时间]", request.expires_at)

        engine = make_engine(directory, clock, actions)
        print("[重启后审批仍存在]", engine.get_approval_request(first.id, "review").request_id == request.request_id)
        approved = engine.approve_step(first.id, "review", True, "测试已核验", actor="reviewer", request_id=request.request_id)
        print("[审核通过后]", approved.status)
        print("[最终状态]", engine.run_until_blocked(first.id).status)

        second = engine.start(make_flow())
        engine.run_until_blocked(second.id)
        old_request = engine.get_approval_request(second.id, "review")
        clock.advance(61)
        expired = engine.expire_approvals(second.id)
        print("[另一轮过期]", expired.status)
        renewed = engine.reissue_approval(second.id, "review")
        new_request = renewed.steps["review"].approval_request
        print("[重发申请编号已变化]", new_request.request_id != old_request.request_id)
        engine.approve_step(second.id, "review", True, "复核确认", actor="reviewer", request_id=new_request.request_id)
        finished = engine.run_until_blocked(second.id)
        events = finished.steps["review"].approval_events
        print("[审批审计历史]", [event.event for event in events])
        print("[第二轮最终状态]", finished.status)


if __name__ == "__main__":
    main()
