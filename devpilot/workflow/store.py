"""以 JSON 原子替换保存每一次 Workflow 的完整状态。"""

import json
import re
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from .model import StepCondition, StepDefinition, StepState, WorkflowDefinition, WorkflowRun
from .approval import ApprovalEvent, ApprovalPolicy, ApprovalRequest
from .recovery import RecoveryEvent, RetryPolicy

_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def json_snapshot(value: Any) -> Any:
    """把对象复制为严格合法的 JSON 数据，防止动作返回不可持久化的对象。"""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def serialize_run(run: WorkflowRun) -> dict[str, Any]:
    """把包含嵌套数据类的工作流状态转换为可写入 JSON 的字典。"""
    return json_snapshot(asdict(run))


def deserialize_run(data: dict[str, Any]) -> WorkflowRun:
    """把磁盘中的工作流 JSON 还原成带有步骤定义和步骤状态的对象。"""
    raw_definition = data["definition"]
    specs = []
    for raw in raw_definition["steps"]:
        condition = StepCondition(**raw["condition"]) if raw["condition"] is not None else None
        policy_data = raw.get("approval")
        policy = ApprovalPolicy(**{**policy_data, "approvers": tuple(policy_data["approvers"])}) if policy_data else None
        specs.append(StepDefinition(
            id=raw["id"], kind=raw["kind"], action=raw["action"],
            depends_on=tuple(raw["depends_on"]),
            allow_skipped_dependencies=tuple(raw["allow_skipped_dependencies"]),
            condition=condition, params=raw["params"], max_attempts=raw["max_attempts"], approval=policy,
            retry=RetryPolicy(**raw["retry"]) if raw.get("retry") is not None else None,
            exclusive_resources=tuple(raw.get("exclusive_resources", ())),
        ))
    definition = WorkflowDefinition(raw_definition["id"], raw_definition["name"], tuple(specs))
    states = {}
    for step_id, raw_state in data["steps"].items():
        raw_state = dict(raw_state)
        if raw_state.get("approval_request") is not None:
            raw_state["approval_request"] = ApprovalRequest(**raw_state["approval_request"])
        raw_state["approval_events"] = [ApprovalEvent(**event) for event in raw_state.get("approval_events", [])]
        raw_state["recovery_events"] = [RecoveryEvent(**event) for event in raw_state.get("recovery_events", [])]
        states[step_id] = StepState(**raw_state)
    return WorkflowRun(
        id=data["id"], definition=definition, inputs=data["inputs"], steps=states,
        status=data["status"], created_at=data["created_at"], updated_at=data["updated_at"],
    )


class WorkflowStore:
    """负责按 Run ID 保存、读取和列出持久化的 Workflow 实例。"""

    def __init__(self, directory: Path):
        """创建保存 Workflow JSON 文件的目录和进程内互斥锁。"""
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, run_id: str) -> Path:
        """校验 Run ID 并将其映射到固定目录中的 JSON 文件，避免路径穿越。"""
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("Invalid workflow run ID")
        return self.directory / f"{run_id}.json"

    def create(self, run: WorkflowRun) -> None:
        """只允许新建不存在的 Workflow 运行记录，避免覆盖旧状态。"""
        with self._lock:
            path = self._path(run.id)
            if path.exists():
                raise FileExistsError(f"Workflow already exists: {run.id}")
            self._write(path, run)

    def save(self, run: WorkflowRun) -> None:
        """将更新后的 Workflow 状态写入临时文件并原子替换旧文件。"""
        with self._lock:
            path = self._path(run.id)
            if not path.exists():
                raise KeyError(f"Unknown workflow run: {run.id}")
            self._write(path, run)

    def get(self, run_id: str) -> WorkflowRun:
        """从磁盘重新读取完整运行记录，防止调用者直接修改内存状态。"""
        with self._lock:
            path = self._path(run_id)
            if not path.is_file():
                raise KeyError(f"Unknown workflow run: {run_id}")
            return deserialize_run(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self) -> list[WorkflowRun]:
        """读取当前状态目录内的所有 Workflow 记录并按创建时间排序。"""
        with self._lock:
            runs = [self.get(path.stem) for path in self.directory.glob("*.json") if _RUN_ID_PATTERN.fullmatch(path.stem)]
            return sorted(runs, key=lambda run: (run.created_at, run.id))

    def _write(self, path: Path, run: WorkflowRun) -> None:
        """先完整序列化再进行同目录临时文件替换，降低写入中断造成的文件损坏风险。"""
        data = serialize_run(run)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(path)
