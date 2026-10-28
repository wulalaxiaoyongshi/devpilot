"""使用临时状态目录演示工作流分支、人工审批和恢复执行。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from devpilot.workflow import (
    ActionRegistry, StepCondition, StepDefinition,
    WorkflowDefinition, WorkflowEngine, WorkflowStore,
)


def analyze(ctx):
    """模拟分析用户需求，返回后续条件分支使用的决策结果。"""
    print("[分析] 需求：", ctx.inputs["request"])
    return {"need_code": ctx.inputs["need_code"]}


def implement(ctx):
    """模拟编写代码步骤并返回可供后续测试使用的文件名称。"""
    print("[编码] 本次需要修改代码")
    return {"file": "src/login.py"}


def explain(ctx):
    """模拟无需修改代码时直接输出调查报告的分支。"""
    print("[说明] 本次只需要解释问题")
    return {"report": "无需修改代码"}


def final_check(ctx):
    """模拟合并两个条件分支后整理最终交付结果的步骤。"""
    print("[汇合] 通过全部已启用分支")
    return {"ready": True}


def publish(ctx):
    """模拟审批完成后的发布操作并返回交付状态。"""
    print("[发布] 已经过人工审批，允许交付")
    return {"delivered": True}


def main():
    """组装工作流、运行到审批节点，再由宿主批准并执行剩余步骤。"""
    actions = ActionRegistry()
    actions.register("demo.analyze", analyze)
    actions.register("demo.implement", implement)
    actions.register("demo.explain", explain)
    actions.register("demo.check", final_check)
    actions.register("demo.publish", publish)

    workflow = WorkflowDefinition("fix-bug", "分析、修改与人工验收", (
        StepDefinition("analyze", action="demo.analyze"),
        StepDefinition("implement", action="demo.implement", depends_on=("analyze",), condition=StepCondition("analyze", "need_code", True)),
        StepDefinition("explain", action="demo.explain", depends_on=("analyze",), condition=StepCondition("analyze", "need_code", False)),
        StepDefinition("check", action="demo.check", depends_on=("implement", "explain"), allow_skipped_dependencies=("implement", "explain")),
        StepDefinition("approval", kind="approval", depends_on=("check",)),
        StepDefinition("publish", action="demo.publish", depends_on=("approval",)),
    ))

    with TemporaryDirectory(prefix="devpilot-v7-") as temp_dir:
        root = Path(temp_dir)
        engine = WorkflowEngine(WorkflowStore(root / "workflows"), actions)
        started = engine.start(workflow, {"request": "修复登录接口", "need_code": True})
        waiting = engine.run_until_blocked(started.id)
        print("[第一轮状态]", waiting.status)
        print("[分支状态]", {name: step.status for name, step in waiting.steps.items()})
        assert waiting.status == "waiting_approval"

        recovered_engine = WorkflowEngine(WorkflowStore(root / "workflows"), actions)
        recovered_engine.approve_step(started.id, "approval", approved=True, note="已审阅变更")
        completed = recovered_engine.run_until_blocked(started.id)
        print("[最终状态]", completed.status)
        print("[执行记录]", {name: (step.status, step.attempts) for name, step in completed.steps.items()})
        assert completed.status == "completed"


if __name__ == "__main__":
    main()
