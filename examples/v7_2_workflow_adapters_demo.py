"""不消耗模型 Token，演示 V7 工作流调用 V1/V3/V4/V5/V6 的适配契约。"""

from tempfile import TemporaryDirectory
from pathlib import Path

from devpilot.workflow import (
    ActionRegistry,
    ExternalOutcome,
    ExternalResolverRegistry,
    StepDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRuntimeAdapters,
    WorkflowStore,
    normalize_mcp_v1_text,
)


def main() -> None:
    """在临时目录构造真实工作流调度器，用可观察的假外部服务演示五层适配与后台等待。"""
    with TemporaryDirectory(prefix="devpilot-v72-") as temp:
        actions = ActionRegistry()
        external = ExternalResolverRegistry()
        adapters = WorkflowRuntimeAdapters(actions, external)
        engine = WorkflowEngine(WorkflowStore(Path(temp) / "runs"), actions, external)
        log: list[str] = []
        job_status = {"job-001": ExternalOutcome("running")}

        def dispatch(name: str, arguments: dict) -> dict:
            """模拟经过权限审批的 V1 Dispatcher，同时演示 MCP 返回内容的分类。"""
            log.append(f"Dispatcher: {name} {arguments}")
            if name.startswith("mcp__"):
                return normalize_mcp_v1_text("[MCP RESULT: add; external untrusted data]\nstructured_content: {\"result\": 7}")
            return {"ok": True, "content": {"analysis": "代码需要测试"}}

        def create_task(data: dict) -> str:
            """模拟 V3 TaskStore 创建长期任务并返回唯一编号。"""
            log.append(f"TaskStore.create: {data}")
            return "task-001"

        def run_assignment(task_id: str, agent_id: str) -> dict:
            """模拟 V4 TeamManager 完成指定 Task 并确认结果。"""
            log.append(f"TeamManager: {agent_id} runs {task_id}")
            return {"ok": True, "commit": "abc123"}

        def submit_job(job_type: str, payload: dict) -> str:
            """模拟 V5 后台作业提交，提交时 Job 尚未完成。"""
            log.append(f"BackgroundJobManager.submit: {job_type} {payload}")
            return "job-001"

        adapters.register_v1_tool(dispatch, allowed_tools={"read_file"})
        adapters.register_v3_task(create_task)
        adapters.register_v4_assignment(run_assignment)
        adapters.register_v5_job(submit_job, lambda job_id: job_status[job_id])
        adapters.register_v6_mcp(dispatch, allowed_tools={"mcp__demo__add"})

        definition = WorkflowDefinition("coding-flow", "示例 Coding Workflow", (
            StepDefinition("inspect", action="v1.tool", params={"tool": "read_file", "arguments": {"path": {"$input": "path"}}}),
            StepDefinition("task", action="v3.task_create", depends_on=("inspect",), params={"task": {"title": "实现功能"}}),
            StepDefinition("code", action="v4.assignment", depends_on=("task",), params={"task_id": {"$step": {"id": "task", "field": "task_id"}}, "agent_id": "coder"}),
            StepDefinition("tests", action="v5.job_submit", depends_on=("code",), params={"job_type": "pytest", "payload": {"task": "unit-tests"}}),
            StepDefinition("mcp", action="v6.mcp_tool", depends_on=("tests",), params={"tool": "mcp__demo__add", "arguments": {"a": 3, "b": 4}}),
        ))

        run = engine.start(definition, {"path": "README.md"})
        first = engine.run_until_blocked(run.id)
        print("[首次运行]", first.status)
        print("[后台测试步骤]", first.steps["tests"].status)
        print("[下游 MCP 步骤]", first.steps["mcp"].status)
        print("[轮询运行中]", engine.poll_external(run.id, "tests").status)
        job_status["job-001"] = ExternalOutcome("succeeded", {"passed": True})
        print("[轮询已完成]", engine.poll_external(run.id, "tests").status)
        final = engine.run_until_blocked(run.id)
        print("[最终状态]", final.status)
        for entry in log:
            print(" -", entry)


if __name__ == "__main__":
    main()
