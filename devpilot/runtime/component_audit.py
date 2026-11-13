"""V9.8：记录实际调用到的组件，而不是把未触发能力误写成已使用。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class ComponentAudit:
    """为每条 V7 Run 保存可重复检查的最小化组件事件。"""

    def __init__(self, state: Path):
        """绑定仓库外审计目录和同进程互斥锁。"""
        self.root = Path(state) / "component_audit"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def record(self, run_id: str, component: str, event: str, **meta: Any) -> None:
        """持久化实际进入运行时的组件事件，不记录提示词、源码或 API 密钥。"""
        if not (len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)):
            raise ValueError("Invalid workflow run ID")
        if component not in {"V0", "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"}:
            raise ValueError("Unknown component")
        if not event or len(event) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for ch in event):
            raise ValueError("Invalid event name")
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "component": component,
                  "event": event, "meta": {k: v for k, v in meta.items() if k in {"step", "mode", "count", "status"}}}
        with self.lock:
            with (self.root / f"{run_id}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summary(self, run_id: str) -> dict[str, Any]:
        """汇总真正出现的组件和事件，并将没有出现的能力明确标成未观测。"""
        path = self.root / f"{run_id}.jsonl"
        events = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        observed = sorted({item["component"] for item in events})
        return {"run_id": run_id, "events_count": len(events), "observed_components": observed,
                "events": events, "not_observed_components": sorted(set(f"V{i}" for i in range(10)) - set(observed)),
                "not_proven_by_this_audit": ["context_compaction", "memory_recall", "skills_execution",
                                              "mailbox_peer_collaboration", "cron", "multi_mcp_server",
                                              "distributed_recovery"]}
