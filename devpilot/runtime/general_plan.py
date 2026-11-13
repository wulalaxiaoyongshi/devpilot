"""V9.6：将任意 Python 项目的开发计划约束为明确文件范围内的 V7 工作流。"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from devpilot.runtime.plan_guard import PlanActionRule, WorkflowPlanGuard
from devpilot.workflow.approval import ApprovalPolicy
from devpilot.workflow.engine import validate_definition
from devpilot.workflow.model import StepDefinition, WorkflowDefinition

_WORKER_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_PRIVATE = frozenset({".git", ".env", ".devpilot", ".venv", "venv", "__pycache__", "node_modules", "secrets", "credentials"})


def checked_python_path(root: Path, value: Any) -> str:
    """只接受仓库内真实且不经过符号链接的 Python 目标文件路径。"""
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Python target must be a short POSIX-style relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix != ".py" or any(part in {".", "..", ""} or part.lower() in _PRIVATE or part.startswith(".") for part in value.split("/")):
        raise ValueError("Unsafe or unsupported Python target")
    base = root.resolve(strict=True)
    target = base.joinpath(*path.parts)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if base in parent.parents):
        raise ValueError("Symbolic links are not allowed in target paths")
    if not target.resolve().is_relative_to(base) or target.is_dir():
        raise ValueError("Target escapes repository")
    return path.as_posix()


def parse_coding_plan(text: str, root: Path, *, max_workers: int = 3) -> dict[str, Any]:
    """将模型 JSON 计划校验为有限的 Worker、任务与互不重叠的 Python 文件集合。"""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 24000:
        raise ValueError("Model plan is not valid bounded text")
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:].strip()
        if not cleaned.endswith("```"):
            raise ValueError("Invalid JSON code fence")
        cleaned = cleaned[:-3].strip()
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError("Model must return one JSON object containing summary and workers") from error
    if not isinstance(raw, dict) or set(raw) != {"summary", "workers"} or not isinstance(raw["summary"], str) or not raw["summary"].strip() or len(raw["summary"]) > 1000:
        raise ValueError("Plan needs a summary and workers")
    workers = raw["workers"]
    if not isinstance(workers, list) or not 1 <= len(workers) <= max_workers:
        raise ValueError("Plan exceeds worker limit")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    result = []
    for row in workers:
        if not isinstance(row, dict) or set(row) != {"id", "task", "files"}:
            raise ValueError("Worker needs id, task, files")
        ident, task, files = row["id"], row["task"], row["files"]
        if not isinstance(ident, str) or not _WORKER_ID.fullmatch(ident) or ident in seen_ids or ident in {"merge", "verify", "report", "preapproval", "delivery_review", "publish_review", "publish_pr", "analysis_report"}:
            raise ValueError("Worker ID is invalid or repeated")
        if not isinstance(task, str) or not task.strip() or len(task) > 3000:
            raise ValueError("Worker task is missing or too long")
        if not isinstance(files, list) or not 1 <= len(files) <= 8:
            raise ValueError("Worker must specify 1-8 Python files")
        safe_files = [checked_python_path(root, filename) for filename in files]
        if len(set(safe_files)) != len(safe_files) or seen_files.intersection(safe_files):
            raise ValueError("Workers may not edit overlapping files")
        seen_ids.add(ident)
        seen_files.update(safe_files)
        result.append({"id": ident, "task": task, "files": safe_files})
    return {"summary": raw["summary"], "workers": result}


def build_general_workflow(plan: dict[str, Any], *, workflow_id: str, approver: str = "reviewer") -> WorkflowDefinition:
    """用 V9.5 PlanGuard 验证动态 Worker 步骤，并添加合并、验收和交付审批关卡。"""
    workers = plan["workers"]
    ids = [worker["id"] for worker in workers]
    guard = WorkflowPlanGuard((
        PlanActionRule("general.worker", frozenset({"worker"}), mutates=True, param_validators={"worker": lambda w: w in workers}),
        PlanActionRule("general.merge", frozenset()),
        PlanActionRule("general.verify", frozenset()),
    ), max_steps=10)
    definition = guard.compile({"steps": [
        *({"id": worker["id"], "action": "general.worker", "params": {"worker": worker}, "depends_on": []} for worker in workers),
        {"id": "merge", "action": "general.merge", "params": {}, "depends_on": ids},
        {"id": "verify", "action": "general.verify", "params": {}, "depends_on": ["merge"]},
    ]}, workflow_id=workflow_id, approvers=(approver,))
    steps = (*definition.steps,
        StepDefinition(id="delivery_review", kind="approval", depends_on=("verify",),
                       approval=ApprovalPolicy(approvers=(approver,), requester="lead", purpose="确认本次代码和实际测试结果可以交付")),
        StepDefinition(id="report", action="general.report", depends_on=("delivery_review",)))
    result = WorkflowDefinition(workflow_id, "动态 Coding Agent 工作流", steps)
    validate_definition(result)
    return result
