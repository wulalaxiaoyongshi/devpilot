"""V9.5：为非幂等 Workflow 操作持久保留结果未知的提交记录。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from devpilot.workflow.model import StepContext
from devpilot.workflow.store import json_snapshot
from devpilot.workflow.recovery import UncertainActionError


class OperationLedger:
    """通过持久化执行意图拒绝重复提交，直到可信宿主核实外部操作结果。"""

    def __init__(self, directory: Path):
        """创建独立的操作账本目录和单进程并发保护。"""
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, key: str) -> Path:
        """把稳定幂等键映射为固定哈希文件名，避免路径注入。"""
        if not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise ValueError("Operation key must be 1..512 characters")
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str) -> dict[str, Any] | None:
        """从磁盘查询操作的最近持久状态，不推断远端执行结果。"""
        path = self._path(key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def begin(self, key: str, *, description: str) -> dict[str, Any]:
        """使用排他文件创建在调用外部服务之前写下唯一执行意图。"""
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError("Operation description required")
        path = self._path(key)
        record = {"key": key, "status": "inflight", "description": description,
                  "created_at": datetime.now(timezone.utc).isoformat(), "evidence": ""}
        with self._lock:
            try:
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as error:
                raise UncertainActionError("Operation already recorded; reconcile before retrying") from error
        return record

    def reconcile(self, key: str, *, status: str, actor: str, evidence: str) -> dict[str, Any]:
        """只允许可信宿主记录核实后的成功或确定未执行，不自动重新提交外部请求。"""
        if status not in {"succeeded", "not_executed", "failed"}:
            raise ValueError("Unsupported reconciliation status")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Actor and evidence required")
        with self._lock:
            record = self.get(key)
            if record is None or record["status"] != "inflight":
                raise ValueError("Only an inflight operation can be reconciled")
            record.update({"status": status, "actor": actor, "evidence": evidence,
                           "decided_at": datetime.now(timezone.utc).isoformat()})
            path = self._path(key)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            return record


def protect_workflow_action(ledger: OperationLedger, handler: Callable[[StepContext], dict[str, Any]]) -> Callable[[StepContext], dict[str, Any]]:
    """给有副作用的 V7 动作包裹先落盘后执行的保护，未知异常触发恢复核实。"""
    def guarded(context: StepContext) -> dict[str, Any]:
        """使用同一个 Workflow 步骤的稳定幂等键确保崩溃后不自动重放。"""
        ledger.begin(context.idempotency_key, description=f"{context.workflow_id}/{context.step_id}")
        try:
            result = handler(context)
        except Exception as error:
            raise UncertainActionError("Side effect outcome is uncertain; reconcile before retry") from error
        try:
            safe_result = json_snapshot(result)
        except (TypeError, ValueError) as error:
            raise UncertainActionError("Protected operation returned invalid JSON") from error
        if not isinstance(safe_result, dict) or safe_result.get("ok") is False:
            raise UncertainActionError("Protected operation did not confirm success")
        ledger.reconcile(context.idempotency_key, status="succeeded", actor="workflow", evidence="Handler returned a valid result")
        return safe_result
    return guarded
