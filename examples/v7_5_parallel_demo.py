"""演示三条 Coding Workflow 分支并发运行，经过汇合与人工审核才结束。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import sleep

from devpilot.workflow import (
    ActionRegistry, ApprovalPolicy, ParallelWorkflowRunner, StepDefinition,
    WorkflowDefinition, WorkflowEngine, WorkflowStore,
)


def main() -> None:
    """运行真实线程池并发的三个独立步骤，并确认它们汇合后需要人工审批。"""
    logs = []
    guard = Lock()
    actions = ActionRegistry()

    def develop(ctx):
        """模拟编写后端、前端或测试代码，并返回各自独立的修改结果。"""
        with guard:
            logs.append(f"启动 {ctx.step_id}")
        sleep(0.12)
        with guard:
            logs.append(f"完成 {ctx.step_id}")
        return {"artifact": f"{ctx.step_id}.patch"}

    def integrate(ctx):
        """等待三个分支全部完成后，再读取它们的输出执行集成步骤。"""
        outputs = sorted(ctx.results[name]["artifact"] for name in ("backend", "frontend", "tests"))
        print("[汇合] 收到补丁：", outputs)
        return {"integrated": True, "patches": outputs}

    actions.register("develop", develop)
    actions.register("integrate", integrate)
    definition = WorkflowDefinition(
        id="coding-parallel",
        name="并行 Coding Workflow",
        steps=(
            StepDefinition("backend", action="develop", exclusive_resources=("repo:backend",)),
            StepDefinition("frontend", action="develop", exclusive_resources=("repo:frontend",)),
            StepDefinition("tests", action="develop", exclusive_resources=("repo:tests",)),
            StepDefinition("integration", action="integrate", depends_on=("backend", "frontend", "tests"), exclusive_resources=("repo:main",)),
            StepDefinition("review", kind="approval", depends_on=("integration",), approval=ApprovalPolicy(approvers=("reviewer",))),
        ),
    )

    with TemporaryDirectory() as directory:
        engine = WorkflowEngine(WorkflowStore(Path(directory) / "workflows"), actions)
        run = engine.start(definition)
        with ParallelWorkflowRunner(engine, max_workers=3) as runner:
            pending = runner.run_until_blocked(run.id, timeout_seconds=5)
            print("[第一阶段状态]", pending.status)
            print("[并发日志]", logs)
            request = engine.get_approval_request(run.id, "review")
            print("[审批编号]", request.request_id)
            engine.approve_step(run.id, "review", True, actor="reviewer", request_id=request.request_id, note="已核实三个分支的结果")
            finished = runner.run_until_blocked(run.id)
            print("[最终状态]", finished.status)
            assert finished.status == "completed"


if __name__ == "__main__":
    main()
