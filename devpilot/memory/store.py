from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


@dataclass
class MemoryItem:
    id: str
    content: str
    category: str
    tags: list[str]
    source: str
    created_at: str
    updated_at: str


class MemoryStore:

    def __init__(self,path: Path,):
        self.path = path.resolve()
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        if not self.path.exists():
            self._save([])

    def all(self) -> list[MemoryItem]:
        raw_items = self._load()
        return [
            MemoryItem(**item)
            for item in raw_items
        ]

    def get(self,memory_id: str) -> MemoryItem | None:
        for item in self.all():
            if item.id == memory_id:
                return item
        return None

    def add(
        self,
        content: str,
        category: str,
        tags: list[str] | None = None,
        source: str = "agent",
    ) -> MemoryItem:
        now = datetime.now(timezone.utc).isoformat()
        item = MemoryItem(
            id=(
                "mem_"
                + uuid4().hex[:12]
            ),
            content=content,
            category=category,
            tags=tags or [],
            source=source,
            created_at=now,
            updated_at=now,
        )
        items = self.all()
        items.append(item)
        self._save_items(items)
        return item

    def update(self,item: MemoryItem) -> None:

        items = self.all()
        item.updated_at = datetime.now(timezone.utc).isoformat()
        updated = []
        found = False
        for existing in items:
            if existing.id == item.id:
                updated.append(item)
                found = True
            else:
                updated.append(existing)
        if not found:
            raise ValueError(
                f"Unknown memory: {item.id}"
            )
        self._save_items(updated)

    def _load(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
            data = json.loads(text)

        except (
            json.JSONDecodeError,
            OSError,
        ) as error:
            raise RuntimeError(
                f"Failed to load memory "
                f"store: {error}"
            ) from error
        if not isinstance(data, list):
            raise RuntimeError(
                "Memory store must "
                "contain a JSON list"
            )
        return data

    def _save_items(self,items: list[MemoryItem]) -> None:
        self._save(
            [
                asdict(item)
                for item in items
            ]
        )

    def _save(self,data: list[dict]) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp.replace(self.path)