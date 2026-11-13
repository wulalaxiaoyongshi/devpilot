"""从可信工作区采集命令、文件和 Git 事实，为 V8.3 留下可检查的证据。"""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .evidence import EvidenceRecord
from .evidence_store import EvidenceStore
from .store import GoalStore


class EvidenceCollector:
    """按 GoalSpec 的验收条件读取实际工作区事实，但从不自行判断目标通过。"""

    def __init__(
        self,
        goals: GoalStore,
        evidence: EvidenceStore,
        workspace: str | Path,
        *,
        allowed_commands: tuple[tuple[str, ...], ...] = (),
        command_timeout: float = 30.0,
        output_limit_bytes: int = 8192,
        max_file_bytes: int = 2 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定 GoalStore、证据目录和可信工作区，并设置命令白名单及采集上限。"""
        self.goals = goals
        self.evidence = evidence
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        if any(not isinstance(command, tuple) or not command or any(not isinstance(arg, str) or not arg for arg in command) for command in allowed_commands):
            raise ValueError("allowed_commands must contain non-empty command tuples")
        if command_timeout <= 0 or output_limit_bytes < 128 or max_file_bytes <= 0:
            raise ValueError("Collector limits must be positive and output cap at least 128 bytes")
        self.allowed_commands = frozenset(allowed_commands)
        self.command_timeout = command_timeout
        self.output_limit_bytes = output_limit_bytes
        self.max_file_bytes = max_file_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        """生成带 UTC 时区的采集时间，供证据追溯。"""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Collector clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).isoformat()

    def _git_state(self) -> tuple[str | None, bool | None]:
        """读取当前 Git HEAD 和工作区脏状态，若不是 Git 仓库则返回未知。"""
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workspace, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5, check=False)
            if head.returncode != 0:
                return None, None
            status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.workspace, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5, check=False)
            return head.stdout.strip(), (bool(status.stdout.strip()) if status.returncode == 0 else None)
        except (OSError, subprocess.TimeoutExpired):
            return None, None

    def _safe_file(self, name: str) -> Path:
        """将 Goal 中的文件路径限制在工作区内，拒绝通过符号链接跳出目录。"""
        candidate = (self.workspace / name).resolve(strict=False)
        if not candidate.is_relative_to(self.workspace):
            raise PermissionError(f"Evidence file escapes workspace: {name}")
        return candidate

    def _file_facts(self, kind: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """只读取指定文件的存在性、大小、哈希及目标文本命中情况。"""
        path = self._safe_file(config["path"])
        if not path.is_file():
            return "collected", {"path": config["path"], "exists": False}
        try:
            size = path.stat().st_size
            if size > self.max_file_bytes:
                return "unknown", {"path": config["path"], "exists": True, "size_bytes": size, "reason": "File exceeds the safe read limit"}
            raw = path.read_bytes()
        except OSError as exc:
            return "unknown", {"path": config["path"], "reason": type(exc).__name__}
        facts: dict[str, Any] = {"path": config["path"], "exists": True, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if kind == "file_contains":
            try:
                facts["contains_target"] = config["contains"] in raw.decode("utf-8")
            except UnicodeDecodeError:
                return "unknown", {**facts, "reason": "File is not UTF-8 text"}
        return "collected", facts

    def _read_tail_capped(self, stream: Any) -> tuple[str, int, bool, str]:
        """只保留日志末尾的有限字节，同时计算完整日志的 SHA256 摘要。"""
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - self.output_limit_bytes))
        text = stream.read(self.output_limit_bytes).decode("utf-8", errors="replace")
        stream.seek(0)
        digest = hashlib.sha256()
        while block := stream.read(65536):
            digest.update(block)
        return text, size, size > self.output_limit_bytes, digest.hexdigest()

    def _command_facts(self, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """仅执行可信宿主预先允许的完整 argv，记录退出码、耗时和有界日志。"""
        argv = tuple(config["argv"])
        if argv not in self.allowed_commands:
            raise PermissionError(f"Command is not approved by the host: {argv!r}")
        start = time.monotonic()
        with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
            try:
                result = subprocess.run(list(argv), cwd=self.workspace, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=self.command_timeout, check=False, shell=False)
                code: int | None = result.returncode
                status = "collected"
                reason: str | None = None
            except subprocess.TimeoutExpired:
                code, status, reason = None, "unknown", "Command timed out; child processes may still exist"
            except OSError as exc:
                code, status, reason = None, "unknown", f"Command could not start: {type(exc).__name__}"
            out, out_size, out_cut, out_hash = self._read_tail_capped(stdout)
            err, err_size, err_cut, err_hash = self._read_tail_capped(stderr)
        facts: dict[str, Any] = {
            "argv": list(argv), "expected_exit_code": config.get("expected_exit_code", 0),
            "returncode": code, "duration_ms": round((time.monotonic() - start) * 1000),
            "stdout_tail": out, "stderr_tail": err, "stdout_bytes": out_size,
            "stderr_bytes": err_size, "stdout_truncated": out_cut, "stderr_truncated": err_cut,
            "stdout_sha256": out_hash, "stderr_sha256": err_hash,
        }
        if reason is not None:
            facts["reason"] = reason
        return status, facts

    def collect(self, run_id: str, criterion_id: str) -> EvidenceRecord:
        """读取指定验收条件的真实事实并追加证据，不修改 Goal 的验收状态。"""
        run = self.goals.get(run_id)
        if run.status != "verifying":
            raise ValueError("Goal must be in verifying state before evidence collection")
        criterion = next((item for item in run.spec.criteria if item.id == criterion_id), None)
        if criterion is None:
            raise KeyError(f"Unknown criterion: {criterion_id}")
        if criterion.kind == "manual":
            raise PermissionError("Manual criterion requires a trusted human evidence source in a later stage")
        before_head, before_dirty = self._git_state()
        if criterion.kind in {"file_exists", "file_contains"}:
            status, observation = self._file_facts(criterion.kind, criterion.config)
        elif criterion.kind == "command_exit_code":
            status, observation = self._command_facts(criterion.config)
        elif criterion.kind == "git_head":
            status = "collected" if before_head is not None else "unknown"
            observation = {"head": before_head} if before_head else {"reason": "Workspace has no readable Git HEAD"}
        else:
            raise ValueError(f"Unsupported evidence kind: {criterion.kind}")
        after_head, after_dirty = self._git_state()
        if before_head != after_head:
            status = "unknown"
            observation["reason"] = "Git HEAD changed during evidence collection"
        spec_json = json.dumps(asdict(run.spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        spec_hash = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
        return self.evidence.append(EvidenceRecord(
            id=uuid4().hex, goal_run_id=run_id, criterion_id=criterion.id, goal_spec_sha256=spec_hash,
            kind=criterion.kind, status=status, captured_at=self._now(),
            workspace=str(self.workspace), git_head_before=before_head,
            git_head_after=after_head, git_dirty_before=before_dirty,
            git_dirty_after=after_dirty, observation=observation,
        ))

    def collect_all(self, run_id: str) -> list[EvidenceRecord]:
        """按 GoalSpec 顺序采集全部自动验收条件，明确跳过需要人工证明的条件。"""
        run = self.goals.get(run_id)
        return [self.collect(run_id, criterion.id) for criterion in run.spec.criteria if criterion.kind != "manual"]
