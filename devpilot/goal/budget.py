"""保存 Goal 的轮次、时间及由可信宿主上报的 Token 和费用预算。"""

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Callable

_ID = re.compile(r"^[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class GoalBudget:
    """定义每个目标可启动的修复轮数及可选的时间、Token 和美元费用上限。"""

    max_rounds: int = 3
    max_elapsed_seconds: float | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        """拒绝非法或没有实际意义的预算配置。"""
        if type(self.max_rounds) is not int or self.max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        if self.max_elapsed_seconds is not None and (type(self.max_elapsed_seconds) not in (float, int) or not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0):
            raise ValueError("max_elapsed_seconds must be positive and finite")
        if self.max_tokens is not None and (type(self.max_tokens) is not int or self.max_tokens < 0):
            raise ValueError("max_tokens must be a nonnegative integer")
        if self.max_cost_usd is not None and (type(self.max_cost_usd) not in (float, int) or not math.isfinite(self.max_cost_usd) or self.max_cost_usd < 0):
            raise ValueError("max_cost_usd must be nonnegative and finite")


@dataclass(frozen=True)
class UsageEvent:
    """保存可信宿主按唯一事件编号上报的一次真实资源使用量。"""

    event_id: str
    tokens: int
    cost_usd: float
    source: str
    recorded_at: str


@dataclass(frozen=True)
class BudgetAccount:
    """记录目标预算及不可重复计费的用量事件，并提供版本号检测旧写入。"""

    goal_run_id: str
    policy: GoalBudget
    started_at: str
    events: tuple[UsageEvent, ...] = field(default_factory=tuple)
    version: int = 0


@dataclass(frozen=True)
class BudgetDecision:
    """说明下一轮修复能否启动、受限原因和剩余资源。"""

    allowed: bool
    reasons: tuple[str, ...]
    used_rounds: int
    used_tokens: int
    used_cost_usd: float
    remaining_rounds: int
    remaining_tokens: int | None
    remaining_cost_usd: float | None
    remaining_seconds: float | None


class BudgetExceededError(RuntimeError):
    """表示当前目标没有足够预算再发起一次修复。"""


class BudgetConflictError(RuntimeError):
    """表示预算已经被其他操作更新，不能用旧版本覆盖。"""


class BudgetStore:
    """用单目标独立 JSON 文件保存预算账本，保证同一实例内写入串行化。"""

    def __init__(self, root: str | Path) -> None:
        """创建预算目录及本实例的读写锁。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, goal_run_id: str) -> Path:
        """检查目标 ID 格式以防止访问任意磁盘路径。"""
        if not isinstance(goal_run_id, str) or not _ID.fullmatch(goal_run_id):
            raise ValueError("Invalid goal run id")
        return self.root / f"{goal_run_id}.json"

    def _validate(self, account: BudgetAccount) -> None:
        """检查预算账号、用量事件及版本字段的安全性。"""
        self._path(account.goal_run_id)
        if not isinstance(account.policy, GoalBudget) or type(account.version) is not int or account.version < 0:
            raise ValueError("Invalid budget account")
        _parse_time(account.started_at)
        seen: set[str] = set()
        for event in account.events:
            if not isinstance(event.event_id, str) or not _EVENT_ID.fullmatch(event.event_id) or event.event_id in seen:
                raise ValueError("Invalid or duplicate usage event ID")
            if type(event.tokens) is not int or event.tokens < 0:
                raise ValueError("Invalid token amount")
            if type(event.cost_usd) not in (float, int) or not math.isfinite(event.cost_usd) or event.cost_usd < 0:
                raise ValueError("Invalid usage cost")
            if not isinstance(event.source, str) or not event.source.strip() or len(event.source) > 256:
                raise ValueError("Invalid usage source")
            _parse_time(event.recorded_at)
            seen.add(event.event_id)

    def _read(self, path: Path) -> BudgetAccount:
        """将磁盘 JSON 恢复为预算账号并核对文件中的目标编号。"""
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        try:
            account = BudgetAccount(
                goal_run_id=raw["goal_run_id"], policy=GoalBudget(**raw["policy"]),
                started_at=raw["started_at"],
                events=tuple(UsageEvent(**item) for item in raw["events"]),
                version=raw["version"],
            )
            self._validate(account)
            if path != self._path(account.goal_run_id):
                raise ValueError("Budget file identity mismatch")
            return account
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid budget data: {exc}") from exc

    def _write(self, account: BudgetAccount) -> None:
        """先同步临时文件后原子替换，降低崩溃造成 JSON 损坏的风险。"""
        self._validate(account)
        path = self._path(account.goal_run_id)
        fd, temporary = tempfile.mkstemp(prefix=".budget-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(account), handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def create(self, goal_run_id: str, policy: GoalBudget, started_at: str) -> BudgetAccount:
        """为新 Goal 建立固定预算，拒绝覆盖既有策略和消耗历史。"""
        with self._lock:
            path = self._path(goal_run_id)
            if path.exists():
                raise FileExistsError("Goal budget already exists")
            account = BudgetAccount(goal_run_id, policy, started_at)
            self._write(account)
            return self._read(path)

    def get(self, goal_run_id: str) -> BudgetAccount:
        """按目标编号读取最新预算与资源用量。"""
        with self._lock:
            return self._read(self._path(goal_run_id))

    def save(self, account: BudgetAccount) -> BudgetAccount:
        """只有版本未过期时才将用量更新写入下一版账本。"""
        with self._lock:
            current = self.get(account.goal_run_id)
            if current.version != account.version:
                raise BudgetConflictError("Budget ledger has changed; reload")
            if current.policy != account.policy or current.started_at != account.started_at:
                raise ValueError("An active budget policy and its start time cannot be changed")
            if account.events[:len(current.events)] != current.events:
                raise ValueError("Usage history must remain append-only")
            from dataclasses import replace
            updated = replace(account, version=current.version + 1)
            self._write(updated)
            return self._read(self._path(account.goal_run_id))


def _parse_time(value: str) -> datetime:
    """统一解析带时区时间戳并转换成 UTC，拒绝无时区记录。"""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


class GoalBudgetManager:
    """结合持久化资源消耗与修复历史，对每次新修复执行预算检查。"""

    def __init__(self, store: BudgetStore, *, clock: Callable[[], datetime] | None = None) -> None:
        """绑定预算存储和可注入时钟，并创建本实例的事件去重锁。"""
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def _now(self) -> datetime:
        """取当前 UTC 时间并验证时钟是否返回合法带时区日期。"""
        result = self._clock()
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("Budget clock must return a timezone-aware datetime")
        return result.astimezone(timezone.utc)

    def open(self, goal_run_id: str, policy: GoalBudget) -> BudgetAccount:
        """为已创建的目标初始化一次不可随意更改的预算计划。"""
        return self.store.create(goal_run_id, policy, self._now().isoformat())

    def record_usage(self, goal_run_id: str, event_id: str, *, tokens: int = 0, cost_usd: float = 0.0, source: str = "host") -> BudgetAccount:
        """按可信上报事件编号累计资源用量，同一事件重复上报不重复计费。"""
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise ValueError("Invalid usage event ID")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a nonnegative integer")
        if type(cost_usd) not in (float, int) or not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("cost_usd must be nonnegative and finite")
        if not isinstance(source, str) or not source.strip() or len(source) > 256:
            raise ValueError("Invalid usage source")
        with self._lock:
            account = self.store.get(goal_run_id)
            for old in account.events:
                if old.event_id == event_id:
                    if old.tokens != tokens or Decimal(str(old.cost_usd)) != Decimal(str(cost_usd)) or old.source != source:
                        raise ValueError("Usage event ID cannot be reused with different values")
                    return account
            from dataclasses import replace
            event = UsageEvent(event_id, tokens, float(cost_usd), source, self._now().isoformat())
            return self.store.save(replace(account, events=account.events + (event,)))

    def check(self, goal_run_id: str, *, used_rounds: int) -> BudgetDecision:
        """检查能否启动下一轮修复，并显示目前真实账本的剩余额度。"""
        if type(used_rounds) is not int or used_rounds < 0:
            raise ValueError("used_rounds must be nonnegative")
        account = self.store.get(goal_run_id)
        policy = account.policy
        now = self._now()
        elapsed = (now - _parse_time(account.started_at)).total_seconds()
        tokens = sum(event.tokens for event in account.events)
        cost = sum((Decimal(str(event.cost_usd)) for event in account.events), Decimal("0"))
        remaining_seconds = None if policy.max_elapsed_seconds is None else max(0.0, policy.max_elapsed_seconds - elapsed)
        remaining_tokens = None if policy.max_tokens is None else max(0, policy.max_tokens - tokens)
        remaining_cost = None if policy.max_cost_usd is None else max(Decimal("0"), Decimal(str(policy.max_cost_usd)) - cost)
        reasons = []
        if elapsed < -1:
            reasons.append("clock_uncertain")
        if used_rounds >= policy.max_rounds:
            reasons.append("round_limit")
        if policy.max_elapsed_seconds is not None and elapsed >= policy.max_elapsed_seconds:
            reasons.append("time_limit")
        if policy.max_tokens is not None and tokens >= policy.max_tokens:
            reasons.append("token_limit")
        if policy.max_cost_usd is not None and cost >= Decimal(str(policy.max_cost_usd)):
            reasons.append("cost_limit")
        return BudgetDecision(
            allowed=not reasons, reasons=tuple(reasons), used_rounds=used_rounds,
            used_tokens=tokens, used_cost_usd=float(cost),
            remaining_rounds=max(0, policy.max_rounds - used_rounds),
            remaining_tokens=remaining_tokens,
            remaining_cost_usd=float(remaining_cost) if remaining_cost is not None else None,
            remaining_seconds=remaining_seconds,
        )
