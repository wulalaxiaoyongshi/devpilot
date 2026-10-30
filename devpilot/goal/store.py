"""把 GoalRun 存入独立 JSON 文件，并通过版本号识别过期写入。"""

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from .model import AcceptanceCriterion, CriterionRecord, GoalEvent, GoalRun, GoalSpec
from .validation import json_snapshot, validate_spec

_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_STATUSES = {"pending", "executing", "verifying", "unknown", "blocked", "repairing", "verified", "exhausted"}
_CRITERION_STATUSES = {"unverified", "pass", "fail", "unknown"}


class GoalConflictError(RuntimeError):
    """表示某个 GoalRun 已被其他写入更新，当前数据版本过期。"""


class GoalStore:
    """管理持久化的目标记录，在同一个 Store 实例内串行读写并原子替换文件。"""

    def __init__(self, root: str | Path) -> None:
        """建立保存 GoalRun 的目录，并创建供本实例使用的线程锁。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, run_id: str) -> Path:
        """把受校验的运行编号映射为固定的 JSON 文件名，阻止路径穿越。"""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must be a 32-character lowercase hexadecimal identifier")
        return self.root / f"{run_id}.json"

    def _encode(self, run: GoalRun) -> dict[str, Any]:
        """将一份 GoalRun 复制成可以安全写入 JSON 的字典。"""
        validate_spec(run.spec)
        if run.status not in _STATUSES or type(run.version) is not int or run.version < 0:
            raise ValueError("GoalRun has invalid status or version")
        if set(run.criteria) != {item.id for item in run.spec.criteria}:
            raise ValueError("Criterion records must match GoalSpec")
        if any(record.status not in _CRITERION_STATUSES for record in run.criteria.values()):
            raise ValueError("Criterion record contains an unknown status")
        if run.status == "verified":
            for criterion in run.spec.criteria:
                record = run.criteria[criterion.id]
                if criterion.required and (record.status != "pass" or not record.evidence_ids):
                    raise ValueError("A verified Goal needs evidence-backed PASS for every required criterion")
        return json_snapshot(asdict(run))

    def _decode(self, raw: dict[str, Any]) -> GoalRun:
        """从 JSON 字典恢复 GoalSpec、验收条件、事件和运行状态。"""
        try:
            definition = raw["spec"]
            criteria = tuple(AcceptanceCriterion(**item) for item in definition["criteria"])
            spec = GoalSpec(id=definition["id"], title=definition["title"], description=definition["description"], criteria=criteria)
            run = GoalRun(
                id=raw["id"], spec=spec, inputs=raw["inputs"],
                criteria={key: CriterionRecord(**value) for key, value in raw["criteria"].items()},
                status=raw["status"], events=[GoalEvent(**event) for event in raw["events"]],
                version=raw["version"], created_at=raw["created_at"], updated_at=raw["updated_at"],
            )
            self._path(run.id)
            self._encode(run)
            return run
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid goal data on disk: {exc}") from exc

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        """先完整写入同目录临时文件，再用 os.replace 原子替换目标文件。"""
        fd, temporary = tempfile.mkstemp(prefix=".goal-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self, path: Path) -> GoalRun:
        """读取指定 JSON 文件并还原成经过校验的 GoalRun。"""
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        run = self._decode(raw)
        if path.stem != run.id:
            raise ValueError("GoalRun ID does not match file name")
        return run

    def create(self, run: GoalRun) -> GoalRun:
        """创建版本为零的新目标文件，拒绝覆盖已有运行记录。"""
        with self._lock:
            path = self._path(run.id)
            if path.exists():
                raise FileExistsError(f"Goal already exists: {run.id}")
            if run.version != 0:
                raise ValueError("New GoalRun must start at version 0")
            payload = self._encode(run)
            self._atomic_write(path, payload)
            return self._read(path)

    def get(self, run_id: str) -> GoalRun:
        """根据运行编号读取一份独立副本，让调用方不能直接修改磁盘记录。"""
        with self._lock:
            return self._read(self._path(run_id))

    def save(self, run: GoalRun) -> GoalRun:
        """仅在磁盘版本与传入版本相同的情况下保存下一版本，拒绝旧状态覆盖新状态。"""
        with self._lock:
            path = self._path(run.id)
            current = self._read(path)
            if current.version != run.version:
                raise GoalConflictError(f"Stale GoalRun: expected {run.version}, current {current.version}")
            payload = self._encode(run)
            payload["version"] = current.version + 1
            self._atomic_write(path, payload)
            return self._read(path)

    def list_runs(self) -> list[GoalRun]:
        """读取所有有效目标文件并按照创建时间与编号排列。"""
        with self._lock:
            runs = [self._read(path) for path in self.root.glob("[0-9a-f]" * 32 + ".json")]
            return sorted(runs, key=lambda run: (run.created_at, run.id))
