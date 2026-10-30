"""独立持久化每次证据采集，保留历史而不覆盖旧结果。"""

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from .evidence import EvidenceRecord
from .validation import json_snapshot

_ID = re.compile(r"^[0-9a-f]{32}$")


class EvidenceStore:
    """将证据按 Goal ID 分类保存成独立 JSON 文件，方便后续验证器追踪。"""

    def __init__(self, root: str | Path) -> None:
        """创建证据根目录和单实例的线程锁。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _goal_dir(self, run_id: str) -> Path:
        """检查 Goal ID 并返回它的独立证据目录。"""
        if not isinstance(run_id, str) or _ID.fullmatch(run_id) is None:
            raise ValueError("Invalid goal run id")
        return self.root / run_id

    def _path(self, run_id: str, evidence_id: str) -> Path:
        """检查证据 ID 并构造不会越过存储根目录的文件路径。"""
        if not isinstance(evidence_id, str) or _ID.fullmatch(evidence_id) is None:
            raise ValueError("Invalid evidence id")
        return self._goal_dir(run_id) / f"{evidence_id}.json"

    def _encode(self, record: EvidenceRecord) -> dict[str, Any]:
        """检查证据的身份和状态，然后转换为可安全持久化的 JSON 字典。"""
        if record.status not in {"collected", "unknown"}:
            raise ValueError("Invalid evidence status")
        if not isinstance(record.criterion_id, str) or not record.criterion_id:
            raise ValueError("Invalid criterion id")
        self._path(record.goal_run_id, record.id)
        return json_snapshot(asdict(record))

    def _read(self, path: Path) -> EvidenceRecord:
        """读取一份证据并校验文件位置与记录身份是否匹配。"""
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        try:
            record = EvidenceRecord(**data)
            self._encode(record)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"Invalid evidence record: {exc}") from exc
        if path != self._path(record.goal_run_id, record.id):
            raise ValueError("Evidence file identity mismatch")
        return record

    def append(self, record: EvidenceRecord) -> EvidenceRecord:
        """将一次采集作为新证据写盘，不允许覆盖已有证据编号。"""
        with self._lock:
            payload = self._encode(record)
            path = self._path(record.goal_run_id, record.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise FileExistsError(f"Evidence already exists: {record.id}")
            fd, temporary = tempfile.mkstemp(prefix=".evidence-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    raise FileExistsError(f"Evidence already exists: {record.id}")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self._read(path)

    def get(self, run_id: str, evidence_id: str) -> EvidenceRecord:
        """按 Goal ID 和证据 ID 读取一份已经保存的采集记录。"""
        with self._lock:
            return self._read(self._path(run_id, evidence_id))

    def list_for_goal(self, run_id: str, criterion_id: str | None = None) -> list[EvidenceRecord]:
        """按采集时间列出目标的全部证据，并可筛选指定验收条件。"""
        with self._lock:
            directory = self._goal_dir(run_id)
            if not directory.exists():
                return []
            records = [self._read(path) for path in directory.glob("[0-9a-f]" * 32 + ".json")]
            if criterion_id is not None:
                records = [item for item in records if item.criterion_id == criterion_id]
            return sorted(records, key=lambda item: (item.captured_at, item.id))
