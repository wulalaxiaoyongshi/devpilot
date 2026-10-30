"""定义 V8.2 的不可变证据结构，只保存观察到的事实，不给出验收结论。"""

from dataclasses import dataclass
from typing import Any, Literal

EvidenceStatus = Literal["collected", "unknown"]


@dataclass(frozen=True)
class EvidenceRecord:
    """记录一次验收条件的数据采集结果，并绑定目标、条件、时间和代码版本。"""

    id: str
    goal_run_id: str
    criterion_id: str
    goal_spec_sha256: str
    kind: str
    status: EvidenceStatus
    captured_at: str
    workspace: str
    git_head_before: str | None
    git_head_after: str | None
    git_dirty_before: bool | None
    git_dirty_after: bool | None
    observation: dict[str, Any]
