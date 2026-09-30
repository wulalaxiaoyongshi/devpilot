import re

from devpilot.memory.store import (
    MemoryItem,
    MemoryStore,
)


def normalize_memory(text: str,) -> str:

    return re.sub(
        r"\s+",
        " ",
        text.strip().lower(),
    )


class MemoryConsolidator:

    def __init__(self,store: MemoryStore):
        self.store = store

    def save(
        self,
        content: str,
        category: str,
        tags: list[str] | None = None,
        source: str = "agent",
    ) -> MemoryItem:

        normalized = normalize_memory(content)
        new_tags = set(tags or [])
        for item in self.store.all():
            if (normalize_memory(item.content)!= normalized):
                continue
            merged_tags = (set(item.tags) | new_tags)
            item.tags = sorted(merged_tags)
            self.store.update(item)
            return item

        return self.store.add(
            content=content,
            category=category,
            tags=sorted(new_tags),
            source=source,
        )