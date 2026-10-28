"""将 V1～V6 既有接口接入 V7，而不在 Workflow 中复制它们的执行逻辑。"""

from collections.abc import Callable, Iterable
from typing import Any

from .actions import ActionRegistry
from .external import ExternalOutcome, ExternalResolverRegistry, ExternalSubmission
from .model import StepContext
from .store import json_snapshot


def resolve_arguments(value: Any, ctx: StepContext) -> Any:
    """只解析明确声明的输入和上游步骤字段引用，不执行任何配置中的表达式。"""
    if isinstance(value, list):
        return [resolve_arguments(item, ctx) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$input"}:
            key = value["$input"]
            if not isinstance(key, str) or key not in ctx.inputs:
                raise KeyError(f"Missing workflow input: {key}")
            return json_snapshot(ctx.inputs[key])
        if set(value) == {"$step"}:
            ref = value["$step"]
            if not isinstance(ref, dict) or set(ref) != {"id", "field"}:
                raise ValueError("$step must declare id and field")
            step_id, field = ref["id"], ref["field"]
            if not isinstance(step_id, str) or not isinstance(field, str) or step_id not in ctx.results or field not in ctx.results[step_id]:
                raise KeyError(f"Missing upstream step output: {step_id}.{field}")
            return json_snapshot(ctx.results[step_id][field])
        return {key: resolve_arguments(item, ctx) for key, item in value.items()}
    return json_snapshot(value)


def _object(value: Any, label: str) -> dict[str, Any]:
    """将动作配置或结果检查为可持久化的 JSON 字典。"""
    safe = json_snapshot(value)
    if not isinstance(safe, dict):
        raise TypeError(f"{label} must be a JSON object")
    return safe


class WorkflowRuntimeAdapters:
    """把各版本原有的公开执行入口注册为 Workflow 动作，并保留执行权限和任务所有权边界。"""

    def __init__(self, actions: ActionRegistry, external: ExternalResolverRegistry | None = None) -> None:
        """接收 V7.1 动作表和外部任务查询表，所有具体 Runtime 通过调用方注入。"""
        self.actions = actions
        self.external = external or ExternalResolverRegistry()

    def register_v1_tool(self, dispatch: Callable[[str, dict[str, Any]], Any], *, allowed_tools: Iterable[str], action_name: str = "v1.tool") -> None:
        """工作流执行受允许的 V1 工具时，必须经宿主注入的 Dispatcher 入口而非直接调用 Tool.handler。"""
        allowed = frozenset(allowed_tools)
        if not allowed or not callable(dispatch):
            raise ValueError("V1 dispatcher and nonempty tool allowlist are required")

        def handle(ctx: StepContext) -> dict[str, Any]:
            """读取步骤指定的工具和参数，经原权限路径执行后记录 JSON 结果。"""
            tool = ctx.params.get("tool")
            if tool not in allowed:
                raise PermissionError(f"Workflow tool not allowed: {tool}")
            args = _object(resolve_arguments(ctx.params.get("arguments", {}), ctx), "tool arguments")
            response = _object(dispatch(tool, args), "V1 dispatcher result")
            if type(response.get("ok")) is not bool:
                raise ValueError("V1 dispatch must return {'ok': bool, 'content': ...}; unclassified text is unsafe")
            if not response["ok"]:
                raise RuntimeError(f"V1 tool failed or was denied: {str(response.get('content', ''))[:1000]}")
            return {"tool": tool, "content": response.get("content")}

        self.actions.register(action_name, handle)

    def register_v3_task(self, create_task: Callable[[dict[str, Any]], Any], *, action_name: str = "v3.task_create") -> None:
        """工作流需要建立长期任务时调用已有 V3 创建入口，只保存任务 ID 而不代替 Task Graph 调度。"""
        if not callable(create_task):
            raise ValueError("V3 task creator must be callable")

        def handle(ctx: StepContext) -> dict[str, Any]:
            """把步骤配置转为任务创建参数，并记录 V3 返回的持久任务编号。"""
            payload = _object(resolve_arguments(ctx.params.get("task", {}), ctx), "task payload")
            task_id = create_task(payload)
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("V3 task creator must return a nonempty task ID")
            return {"task_id": task_id, "created": True}

        self.actions.register(action_name, handle)

    def register_v3_runnable(self, list_runnable: Callable[[], list[str]], *, action_name: str = "v3.runnable") -> None:
        """工作流需要查看当前可执行任务时查询既有 V3 Task Graph，而不复制依赖判断规则。"""
        if not callable(list_runnable):
            raise ValueError("V3 runnable query must be callable")

        def handle(ctx: StepContext) -> dict[str, Any]:
            """读取 V3 已判定为可执行的 Task ID 列表并保存查询结果。"""
            task_ids = list_runnable()
            if not isinstance(task_ids, list) or any(not isinstance(task_id, str) for task_id in task_ids):
                raise TypeError("V3 runnable query must return a list of task IDs")
            return {"task_ids": json_snapshot(task_ids)}

        self.actions.register(action_name, handle)

    def register_v4_assignment(self, run_assignment: Callable[[str, str], Any], *, action_name: str = "v4.assignment") -> None:
        """工作流要求指定 Worker 执行某 Task 时委托 V4 TeamManager，任务认领与 Worktree 仍归 V4 负责。"""
        if not callable(run_assignment):
            raise ValueError("V4 team assignment callback must be callable")

        def handle(ctx: StepContext) -> dict[str, Any]:
            """解析任务 ID 和队友 ID，等待现有 V4 执行接口返回后再保存结果。"""
            task_id = resolve_arguments(ctx.params.get("task_id"), ctx)
            agent_id = resolve_arguments(ctx.params.get("agent_id"), ctx)
            if not isinstance(task_id, str) or not isinstance(agent_id, str) or not task_id or not agent_id:
                raise ValueError("V4 assignment requires task_id and agent_id")
            response = _object(run_assignment(task_id, agent_id), "V4 assignment result")
            if response.get("ok") is not True:
                raise RuntimeError(f"V4 assignment did not confirm success: {response!r}")
            return {"task_id": task_id, "agent_id": agent_id, "result": response}

        self.actions.register(action_name, handle)

    def register_v5_job(self, submit_job: Callable[[str, dict[str, Any]], str], query_job: Callable[[str], ExternalOutcome], *, provider: str = "v5.jobs", action_name: str = "v5.job_submit") -> None:
        """工作流提交 V5 Job 后进入 waiting_external，并在后续轮询中查询真实完成状态。"""
        if not callable(submit_job) or not callable(query_job):
            raise ValueError("V5 submitter and job status reader must be callable")

        def handle(ctx: StepContext) -> ExternalSubmission:
            """把步骤的作业类型和参数交给 V5，并把 Job ID 作为外部等待凭据持久化。"""
            job_type = ctx.params.get("job_type")
            if not isinstance(job_type, str) or not job_type:
                raise ValueError("V5 job_type is required")
            payload = _object(resolve_arguments(ctx.params.get("payload", {}), ctx), "job payload")
            job_id = submit_job(job_type, payload)
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("V5 submitter must return a nonempty job ID")
            return ExternalSubmission(provider=provider, reference=job_id)

        def resolve(job_id: str, metadata: dict[str, Any]) -> ExternalOutcome:
            """查询 V5 JobStore 的最新状态，确保提交动作不会被误判成执行完成。"""
            return query_job(job_id)

        self.external.register(provider, resolve)
        self.actions.register(action_name, handle)

    def register_v6_mcp(self, dispatch: Callable[[str, dict[str, Any]], Any], *, allowed_tools: Iterable[str], action_name: str = "v6.mcp_tool") -> None:
        """MCP 步骤也经 V1 Dispatcher 和 MCP Permission 执行，绝不直接调用远端工具 Handler。"""
        names = frozenset(allowed_tools)
        if not names or any(not isinstance(item, str) or not item.startswith("mcp__") for item in names):
            raise ValueError("V6 allowlist must contain full mcp__ server-namespaced tools")
        self.register_v1_tool(dispatch, allowed_tools=names, action_name=action_name)


def normalize_mcp_v1_text(text: str) -> dict[str, Any]:
    """把 V6 MCP 格式化结果中的明确成功标记转为 V7 可判定的结构化执行状态。"""
    if not isinstance(text, str):
        raise TypeError("MCP V1 Dispatcher result must be text")
    return {"ok": text.startswith("[MCP RESULT: "), "content": text[:12000]}
