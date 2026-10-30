"""把每轮修复的意图、发起状态和 Workflow ID 单独保存为可审计 JSON。"""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from .feedback import FeedbackItem, RepairFeedback

_ID = re.compile(r"^[0-9a-f]{32}$")
RepairStatus = Literal["prepared", "dispatching", "running", "completed", "failed"]


@dataclass(frozen=True)
class RepairTicket:
    """记录一轮修复从准备、提交到结束的状态及 Workflow 引用。"""

    id: str
    goal_run_id: str
    round_number: int
    feedback: RepairFeedback
    status: RepairStatus
    workflow_run_id: str | None
    created_at: str
    updated_at: str
    error: str = ""
    version: int = 0


class RepairConflictError(RuntimeError):
    """表示修复单已经被其他操作修改，需要重新读取。"""


class RepairStore:
    """使用原子 JSON 文件和实例锁保存修复单，并检查版本冲突。"""

    def __init__(self, root: str | Path) -> None:
        """创建修复单目录与本实例的线程锁。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, goal_id: str, ticket_id: str) -> Path:
        """校验目标和修复单编号，避免通过文件名访问其他目录。"""
        if not isinstance(goal_id, str) or not _ID.fullmatch(goal_id):
            raise ValueError("Invalid goal id")
        if not isinstance(ticket_id, str) or not _ID.fullmatch(ticket_id):
            raise ValueError("Invalid ticket id")
        return self.root / goal_id / f"{ticket_id}.json"

    def _decode(self, raw: dict) -> RepairTicket:
        """将磁盘上的 JSON 恢复成类型明确的修复单和反馈记录。"""
        feedback = raw["feedback"]
        parsed = RepairFeedback(
            goal_run_id=feedback["goal_run_id"], goal_version=feedback["goal_version"],
            goal_title=feedback["goal_title"], created_at=feedback["created_at"],
            failures=tuple(FeedbackItem(**x) for x in feedback["failures"]),
            unknowns=tuple(FeedbackItem(**x) for x in feedback["unknowns"]),
            passes=tuple(FeedbackItem(**x) for x in feedback["passes"]),
        )
        ticket = RepairTicket(**{**raw, "feedback": parsed})
        self._validate(ticket)
        return ticket

    def _validate(self, ticket: RepairTicket) -> None:
        """检查修复单状态、编号、轮次、Workflow 引用和版本。"""
        self._path(ticket.goal_run_id, ticket.id)
        if ticket.feedback.goal_run_id != ticket.goal_run_id:
            raise ValueError("Repair feedback does not match ticket goal")
        if ticket.status not in {"prepared", "dispatching", "running", "completed", "failed"}:
            raise ValueError("Invalid repair status")
        if type(ticket.round_number) is not int or ticket.round_number < 1:
            raise ValueError("Invalid repair round")
        if type(ticket.version) is not int or ticket.version < 0:
            raise ValueError("Invalid repair version")
        if ticket.status in {"running", "completed"} and not ticket.workflow_run_id:
            raise ValueError("Running repair needs a Workflow ID")
        if ticket.workflow_run_id is not None and not isinstance(ticket.workflow_run_id, str):
            raise ValueError("Invalid Workflow ID")

    def _read(self, path: Path) -> RepairTicket:
        """从独立 JSON 文件读取一份修复单并核对其身份。"""
        with path.open("r", encoding="utf-8") as stream:
            result = self._decode(json.load(stream))
        if path != self._path(result.goal_run_id, result.id):
            raise ValueError("Repair file identity mismatch")
        return result

    def _write(self, ticket: RepairTicket) -> None:
        """先写临时文件再原子替换正式修复单。"""
        self._validate(ticket)
        path = self._path(ticket.goal_run_id, ticket.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".repair-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(ticket), stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def create(self, goal_id: str, round_number: int, feedback: RepairFeedback) -> RepairTicket:
        """为指定目标新建修复单，避免覆盖已经存在的修复尝试。"""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            ticket = RepairTicket(uuid4().hex, goal_id, round_number, feedback, "prepared", None, now, now)
            self._write(ticket)
            return self.get(goal_id, ticket.id)

    def get(self, goal_id: str, ticket_id: str) -> RepairTicket:
        """按目标编号和修复单编号查询最新状态。"""
        with self._lock:
            return self._read(self._path(goal_id, ticket_id))

    def list_for_goal(self, goal_id: str) -> list[RepairTicket]:
        """按轮次顺序列出一个 Goal 的全部修复尝试。"""
        with self._lock:
            directory = self.root / goal_id
            self._path(goal_id, "0" * 32)
            if not directory.exists():
                return []
            records = [self._read(path) for path in directory.glob("[0-9a-f]" * 32 + ".json")]
            return sorted(records, key=lambda item: (item.round_number, item.created_at))

    def update(self, ticket: RepairTicket, *, status: RepairStatus, workflow_run_id: str | None = None, error: str = "") -> RepairTicket:
        """仅用最新版本推进修复单状态，拒绝过期请求覆盖新状态。"""
        with self._lock:
            old = self.get(ticket.goal_run_id, ticket.id)
            if old.version != ticket.version:
                raise RepairConflictError("Repair ticket changed; reload before updating")
            allowed = {"prepared": {"dispatching", "failed"}, "dispatching": {"running", "failed"}, "running": {"completed", "failed"}, "completed": set(), "failed": set()}
            if status not in allowed[old.status]:
                raise ValueError(f"Illegal repair transition: {old.status} -> {status}")
            updated = replace(
                ticket, status=status, workflow_run_id=workflow_run_id if workflow_run_id is not None else old.workflow_run_id,
                error=error[:1000], updated_at=datetime.now(timezone.utc).isoformat(), version=ticket.version + 1,
            )
            self._write(updated)
            return self.get(updated.goal_run_id, updated.id)
