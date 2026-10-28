"""提供一条覆盖工具、任务、Worker、Git 合并、后台测试、MCP 与人工审批的工作流蓝图。"""

from __future__ import annotations

from .actions import ActionRegistry
from .model import StepContext, StepDefinition, WorkflowDefinition
from .approval import ApprovalPolicy


def register_coding_verification(actions: ActionRegistry) -> None:
    """注册一个只依据后台测试证据与上游结果决定是否通过的确定性检查动作。"""

    def verify(ctx: StepContext) -> dict:
        """检查 V5 测试已明确成功、Git 合并结果存在且 MCP 查询结果非空，否则阻止审批。"""
        test = ctx.results["test_job"]["result"]
        merge = ctx.results["merge"]
        docs = ctx.results["docs"]["content"]
        if test.get("passed") is not True:
            raise ValueError("Coding tests did not confirm success")
        if not merge.get("merged") or not merge.get("commits"):
            raise ValueError("Git merge evidence is missing")
        if not docs:
            raise ValueError("MCP evidence is missing")
        return {"verified": True, "report": "代码已合并，测试通过，外部资料已查询"}

    actions.register("coding.verify", verify)


def build_coding_workflow(*, reviewer: str = "reviewer") -> WorkflowDefinition:
    """创建包含真实依赖汇合、后台测试和人工交付审批的可持久化 Coding Workflow。"""
    if not reviewer.strip():
        raise ValueError("Reviewer must be a nonempty trusted identity")
    return WorkflowDefinition(
        id="coding-workflow-v76",
        name="并行编码→Git 合并→后台测试→人工审批→交付",
        steps=(
            StepDefinition("inspect", action="v1.tool", params={"tool": "read_file", "arguments": {"path": {"$input": "input_file"}}}),
            StepDefinition("task", action="v3.task_create", depends_on=("inspect",), params={"task": {"title": "实现并验证代码功能"}}),
            StepDefinition("backend", action="v4.assignment", depends_on=("task",), params={"task_id": {"$step": {"id": "task", "field": "task_id"}}, "agent_id": "backend"}, exclusive_resources=("repo:backend",)),
            StepDefinition("qa", action="v4.assignment", depends_on=("task",), params={"task_id": {"$step": {"id": "task", "field": "task_id"}}, "agent_id": "qa"}, exclusive_resources=("repo:qa",)),
            StepDefinition("docs", action="v6.mcp_tool", depends_on=("inspect",), params={"tool": "mcp__docs__search", "arguments": {"q": {"$input": "query"}}}),
            StepDefinition("merge", action="coding.merge", depends_on=("backend", "qa"), exclusive_resources=("repo:main",)),
            StepDefinition("test_job", action="v5.job_submit", depends_on=("merge",), params={"job_type": "pytest", "payload": {"target": "unit"}}),
            StepDefinition("verify", action="coding.verify", depends_on=("test_job", "docs")),
            StepDefinition("review", kind="approval", depends_on=("verify",), approval=ApprovalPolicy(approvers=(reviewer,), requester="agent", timeout_seconds=600, purpose="确认提交前的测试和 Git 合并证据")),
            StepDefinition("deliver", action="v1.tool", depends_on=("review",), params={"tool": "write_file", "arguments": {"path": "DELIVERED.txt", "content": {"$step": {"id": "verify", "field": "report"}}}}, exclusive_resources=("repo:main",)),
        ),
    )
