"""V9.5：把模型提出的步骤草案约束为可信动作集合内的 V7 DAG。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from devpilot.workflow.approval import ApprovalPolicy
from devpilot.workflow.engine import validate_definition
from devpilot.workflow.model import StepDefinition, WorkflowDefinition

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class PlanActionRule:
    """由宿主声明某个可执行动作的允许参数和是否具有副作用。"""

    name: str
    allowed_params: frozenset[str]
    mutates: bool = False
    param_validators: dict[str, Callable[[Any], bool]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """阻止不合法的动作名和参数名进入模型计划校验器。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Action rule needs a name")
        if any(not isinstance(key, str) or not key.isidentifier() for key in self.allowed_params):
            raise ValueError("Invalid action parameter name")
        if type(self.mutates) is not bool:
            raise TypeError("mutates must be boolean")
        if not set(self.param_validators).issubset(self.allowed_params) or any(not callable(check) for check in self.param_validators.values()):
            raise ValueError("Parameter validators must match allowed parameters")


class WorkflowPlanGuard:
    """仅允许模型决定步骤顺序与宿主预先开放的动作参数，并插入写前审批。"""

    def __init__(self, actions: tuple[PlanActionRule, ...], *, max_steps: int = 12, max_bytes: int = 16384):
        """绑定宿主动作契约和模型计划规模上限。"""
        if not actions or len({item.name for item in actions}) != len(actions):
            raise ValueError("Need distinct trusted actions")
        if type(max_steps) is not int or not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between 1 and 100")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 100000:
            raise ValueError("max_bytes must be between 1024 and 100000")
        self.actions = {item.name: item for item in actions}
        self.max_steps = max_steps
        self.max_bytes = max_bytes

    def compile(self, plan: dict[str, Any], *, workflow_id: str, approvers: tuple[str, ...] = ("reviewer",)) -> WorkflowDefinition:
        """严格验证模型计划的类型、参数、依赖和环路，并在写操作之前插入人工审批。"""
        if not isinstance(plan, dict) or set(plan) != {"steps"} or not isinstance(plan["steps"], list):
            raise ValueError("Plan must contain only a steps array")
        try:
            payload = json.dumps(plan, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Plan must be finite JSON") from error
        if len(payload.encode("utf-8")) > self.max_bytes or not 1 <= len(plan["steps"]) <= self.max_steps:
            raise ValueError("Plan exceeds step or byte limit")
        if not isinstance(workflow_id, str) or not _ID.fullmatch(workflow_id):
            raise ValueError("Invalid workflow ID")
        steps: list[StepDefinition] = []
        has_writes = False
        for raw in plan["steps"]:
            if not isinstance(raw, dict) or set(raw) != {"id", "action", "params", "depends_on"}:
                raise ValueError("Step must declare exactly id, action, params and depends_on")
            step_id, action = raw["id"], raw["action"]
            if not isinstance(step_id, str) or not _ID.fullmatch(step_id) or not isinstance(action, str) or action not in self.actions:
                raise ValueError("Invalid step ID or untrusted action")
            params, parents = raw["params"], raw["depends_on"]
            if not isinstance(params, dict) or any(type(key) is not str for key in params) or not isinstance(parents, list) or any(not isinstance(p, str) for p in parents):
                raise ValueError("Invalid step arguments or dependencies")
            rule = self.actions[action]
            if not set(params).issubset(rule.allowed_params):
                raise PermissionError("Model supplied unapproved action parameters")
            for key, value in params.items():
                validator = rule.param_validators.get(key)
                if validator is not None:
                    try:
                        accepted = validator(value) is True
                    except Exception:
                        accepted = False
                    if not accepted:
                        raise PermissionError(f"Unapproved value for action parameter: {key}")
            has_writes |= rule.mutates
            steps.append(StepDefinition(id=step_id, action=action, params=params, depends_on=tuple(parents)))
        if has_writes:
            if not approvers or any(not a or not isinstance(a, str) for a in approvers):
                raise ValueError("Mutating workflows need trusted approvers")
            if any(step.id == "preapproval" for step in steps):
                raise ValueError("Reserved step ID: preapproval")
            steps = [StepDefinition(id="preapproval", kind="approval", approval=ApprovalPolicy(
                approvers=approvers, requester="lead", purpose="批准执行模型规划中的实际写入操作"))] + [
                StepDefinition(id=step.id, action=step.action, params=step.params,
                               depends_on=("preapproval",) + step.depends_on if self.actions[step.action].mutates else step.depends_on)
                for step in steps
            ]
        definition = WorkflowDefinition(id=workflow_id, name="经可信动作规则校验的计划", steps=tuple(steps))
        validate_definition(definition)
        return definition
