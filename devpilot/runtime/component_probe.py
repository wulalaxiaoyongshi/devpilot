"""V10.0：对真实 Lead Agent 的已有公开方法进行短暂、无内容的运行时观测。"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class LeadComponentProbe:
    """仅计数运行时真正进入的方法；不保存 Prompt、工具参数与响应。"""

    context_prepares: int = 0
    memory_recalls: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_returned: Counter[str] = field(default_factory=Counter)

    def snapshot(self) -> dict[str, Any]:
        """输出不含敏感内容、可存储在原生 Workflow Session 内的统计快照。"""
        return {
            "context_prepares": self.context_prepares,
            "memory_recalls": self.memory_recalls,
            "tools_called": dict(sorted(self.tool_calls.items())),
            "tools_returned": dict(sorted(self.tool_returned.items())),
        }


@contextmanager
def observe_lead(lead: Any) -> Iterator[LeadComponentProbe]:
    """临时包装真实 Lead 的 Context/Memory/Dispatcher 方法，结束时恢复原方法。"""
    probe = LeadComponentProbe()
    original_prepare = lead.context.prepare
    original_recall = lead.memory.recall
    original_execute = lead.dispatcher.execute

    def prepare(messages: Any) -> Any:
        """只有原 V2 prepare 返回成功时才计数。"""
        result = original_prepare(messages)
        probe.context_prepares += 1
        return result

    def recall(query: str, limit: int = 5) -> str:
        """调用原 V3 MemoryRecall，观察实际查询但不记录查询词。"""
        result = original_recall(query, limit=limit)
        probe.memory_recalls += 1
        return result

    def execute(call: Any) -> Any:
        """记录原 V1 Dispatcher 的工具名称和是否返回，不记录参数或工具返回正文。"""
        name = getattr(call, "name", "")
        if isinstance(name, str) and name and len(name) <= 96 and all(
            c.isascii() and (c.isalnum() or c in "_-.:") for c in name
        ):
            probe.tool_calls[name] += 1
        result = original_execute(call)
        if name in probe.tool_calls:
            probe.tool_returned[name] += 1  # 返回不等于执行成功；权限拒绝也可能正常返回
        return result

    lead.context.prepare = prepare
    lead.memory.recall = recall
    lead.dispatcher.execute = execute
    try:
        yield probe
    finally:
        lead.context.prepare = original_prepare
        lead.memory.recall = original_recall
        lead.dispatcher.execute = original_execute
