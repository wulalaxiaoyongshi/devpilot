"""用 V8.2 的真实证据逐项验收 Goal，拒绝陈旧证据和不确定的代码版本。"""

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from .evidence import EvidenceRecord
from .evidence_store import EvidenceStore
from .model import AcceptanceCriterion, CriterionRecord, GoalEvent, GoalRun
from .store import GoalStore

Verdict = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class CriterionVerdict:
    """保存一条验收条件的结论、原因以及被使用的证据编号。"""

    criterion_id: str
    required: bool
    status: Verdict
    reason: str
    evidence_id: str | None


@dataclass(frozen=True)
class GoalVerificationReport:
    """保存一次 Goal 验收的整体结果以及每条验收条件的结论。"""

    goal_run_id: str
    status: str
    git_head: str | None
    checked_at: str
    criteria: tuple[CriterionVerdict, ...]


class GoalVerifier:
    """从证据库读取事实，在同一 Git 版本下验证规则并更新 GoalStore。"""

    def __init__(
        self,
        goals: GoalStore,
        evidence: EvidenceStore,
        workspace: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定目标、证据和可信代码目录，为验收创建独立的调用锁。"""
        self.goals = goals
        self.evidence = evidence
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Verification workspace must be a directory")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def _now(self) -> str:
        """读取带时区的 UTC 时间，作为验收记录的可信时间来源。"""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Verification clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).isoformat()

    def _git_state(self) -> tuple[str | None, bool | None]:
        """读取 Git HEAD 和未提交修改状态，无法确认时统一视为未知。"""
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.workspace,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=5,
            )
            if head.returncode != 0:
                return None, None
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.workspace,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=5,
            )
            if status.returncode != 0:
                return None, None
            return head.stdout.strip(), bool(status.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            return None, None

    @staticmethod
    def _spec_hash(run: GoalRun) -> str:
        """按照 V8.2 相同的序列化规则计算 GoalSpec 指纹。"""
        data = json.dumps(asdict(run.spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    @staticmethod
    def _since(run: GoalRun) -> datetime | None:
        """找出最近一轮验证开始的时间，避免直接沿用上轮的旧证据。"""
        for event in reversed(run.events):
            if event.event_type in {"verification_requested", "verification_resumed"}:
                try:
                    when = datetime.fromisoformat(event.at)
                    if when.tzinfo is not None and when.utcoffset() is not None:
                        return when.astimezone(timezone.utc)
                except ValueError:
                    return None
        return None

    def _provenance_problem(
        self,
        evidence: EvidenceRecord,
        criterion: AcceptanceCriterion,
        run: GoalRun,
        spec_hash: str,
        head: str | None,
        dirty: bool | None,
        since: datetime | None,
    ) -> str | None:
        """确认一份证据对应当前 Goal、条件、工作区、验收轮次和干净的 Git 版本。"""
        if evidence.goal_run_id != run.id or evidence.criterion_id != criterion.id:
            return "证据不属于当前目标或验收条件"
        if evidence.kind != criterion.kind or evidence.goal_spec_sha256 != spec_hash:
            return "证据类型或目标定义指纹不匹配"
        if evidence.workspace != str(self.workspace):
            return "证据来自其他工作目录"
        if head is None or dirty is not False:
            return "当前 Git 版本未知或工作区有未提交修改"
        if evidence.git_head_before != head or evidence.git_head_after != head:
            return "证据对应的 Git HEAD 已变化"
        if evidence.git_dirty_before is not False or evidence.git_dirty_after is not False:
            return "采集证据时工作区有未提交修改或状态未知"
        if evidence.status != "collected":
            return "采集时没有得到确定结果"
        try:
            captured = datetime.fromisoformat(evidence.captured_at)
            if captured.tzinfo is None or captured.utcoffset() is None:
                return "证据时间缺少时区"
            if since is None or captured.astimezone(timezone.utc) < since:
                return "证据采集时间早于本轮验收"
        except (ValueError, TypeError):
            return "证据时间格式无效"
        return None

    @staticmethod
    def _compare(criterion: AcceptanceCriterion, observation: dict[str, Any]) -> tuple[Verdict, str]:
        """按照明确的验收规则比较采集事实，而不让模型自行判定成功。"""
        kind, config = criterion.kind, criterion.config
        if not isinstance(observation, dict):
            return "unknown", "证据格式无效"
        if kind == "command_exit_code":
            if observation.get("argv") != config["argv"] or observation.get("expected_exit_code") != config.get("expected_exit_code", 0):
                return "unknown", "证据使用的命令或期望退出码不一致"
            code = observation.get("returncode")
            if type(code) is not int:
                return "unknown", "没有得到明确的进程退出码"
            return ("pass", "退出码符合验收标准") if code == config.get("expected_exit_code", 0) else ("fail", f"实际退出码 {code} 不符合期望")
        if kind == "file_exists":
            if observation.get("path") != config["path"] or type(observation.get("exists")) is not bool:
                return "unknown", "文件路径或存在性证据无效"
            return ("pass", "目标文件存在") if observation["exists"] else ("fail", "目标文件不存在")
        if kind == "file_contains":
            if observation.get("path") != config["path"]:
                return "unknown", "证据文件路径与验收配置不匹配"
            if observation.get("exists") is False:
                return "fail", "目标文件不存在"
            if observation.get("exists") is not True or type(observation.get("contains_target")) is not bool:
                return "unknown", "没有得到可信的文件内容匹配结果"
            return ("pass", "文件包含目标文本") if observation["contains_target"] else ("fail", "文件不包含目标文本")
        if kind == "git_head":
            git_head = observation.get("head")
            if not isinstance(git_head, str) or len(git_head) != 40 or any(c not in "0123456789abcdefABCDEF" for c in git_head):
                return "unknown", "没有得到完整的 Git HEAD"
            return ("pass", "Git HEAD 符合预期") if git_head.lower().startswith(config["expected_sha"].lower()) else ("fail", "Git HEAD 与预期版本不同")
        return "unknown", "人工验收条件不能由自动证据决定"

    def _verify_one(
        self,
        criterion: AcceptanceCriterion,
        run: GoalRun,
        records: list[EvidenceRecord],
        spec_hash: str,
        head: str | None,
        dirty: bool | None,
        since: datetime | None,
    ) -> CriterionVerdict:
        """只用当前验收项最新一份证据判断通过、失败或未知。"""
        if criterion.kind == "manual":
            return CriterionVerdict(criterion.id, criterion.required, "unknown", "人工条件需可信审批来源，V8.3 不自动放行", None)
        if not records:
            return CriterionVerdict(criterion.id, criterion.required, "unknown", "缺少验收证据", None)
        record = records[-1]
        problem = self._provenance_problem(record, criterion, run, spec_hash, head, dirty, since)
        if problem:
            return CriterionVerdict(criterion.id, criterion.required, "unknown", problem, record.id)
        result, reason = self._compare(criterion, record.observation)
        return CriterionVerdict(criterion.id, criterion.required, result, reason, record.id)

    def verify(self, run_id: str, *, actor: str = "goal_verifier") -> GoalVerificationReport:
        """读取最新证据、对照 Git 和验收标准，保存逐项结论并只在全部必要条件通过时认证 Goal。"""
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("Verifier actor cannot be empty")
        with self._lock:
            run = self.goals.get(run_id)
            if run.status != "verifying":
                raise ValueError("Goal must be verifying before it can be verified")
            start_head, start_dirty = self._git_state()
            records = self.evidence.list_for_goal(run_id)
            by_criterion: dict[str, list[EvidenceRecord]] = {}
            for record in records:
                by_criterion.setdefault(record.criterion_id, []).append(record)
            fingerprint = self._spec_hash(run)
            since = self._since(run)
            decisions = tuple(
                self._verify_one(criterion, run, by_criterion.get(criterion.id, []), fingerprint, start_head, start_dirty, since)
                for criterion in run.spec.criteria
            )
            end_head, end_dirty = self._git_state()
            if (start_head, start_dirty) != (end_head, end_dirty):
                decisions = tuple(
                    CriterionVerdict(item.criterion_id, item.required, "unknown", "验收过程中 Git 状态发生变化", item.evidence_id)
                    for item in decisions
                )
            required = [item for item in decisions if item.required]
            if all(item.status == "pass" for item in required):
                next_status = "verified"
            elif any(item.status == "fail" for item in required):
                next_status = "verifying"  # V8.4 才决定是否安排下一轮修复
            else:
                next_status = "unknown"
            now = self._now()
            old_status = run.status
            for item in decisions:
                run.criteria[item.criterion_id] = CriterionRecord(
                    status=item.status,
                    evidence_ids=[item.evidence_id] if item.evidence_id is not None else [],
                    checked_at=now,
                    reason=item.reason,
                )
            run.status = next_status
            run.updated_at = now
            summary = ", ".join(f"{item.criterion_id}={item.status}" for item in decisions)
            run.events.append(GoalEvent(now, "goal_verified" if next_status == "verified" else "verification_checked", actor, summary, old_status, next_status))
            self.goals.save(run)
            return GoalVerificationReport(run.id, next_status, end_head, now, decisions)
