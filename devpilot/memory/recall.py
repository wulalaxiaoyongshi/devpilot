import re

from devpilot.memory.store import (
    MemoryItem,
    MemoryStore,
)


def _tokens(
    text: str,
) -> set[str]:

    text = text.lower()

    tokens = set(
        re.findall(
            r"[a-z0-9_]+",
            text,
        )
    )

    chinese_blocks = re.findall(
        r"[\u4e00-\u9fff]+",
        text,
    )

    for block in chinese_blocks:

        tokens.add(block)

        if len(block) == 1:
            tokens.add(block)
            continue

        for index in range(
            len(block) - 1
        ):
            tokens.add(
                block[
                    index:index + 2
                ]
            )

    return tokens


class MemoryRecall:

    def __init__(self,store: MemoryStore):
        self.store = store

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[MemoryItem]:

        query_tokens = _tokens(
            query
        )

        if not query_tokens:
            return []

        scored: list[
            tuple[float, MemoryItem]
        ] = []

        for item in self.store.all():

            memory_tokens = _tokens(
                " ".join(
                    [
                        item.content,
                        item.category,
                        *item.tags,
                    ]
                )
            )

            overlap = (
                query_tokens
                & memory_tokens
            )

            if not overlap:
                continue

            score = float(
                len(overlap)
            )

            tag_tokens = _tokens(
                " ".join(
                    item.tags
                )
            )

            score += (
                len(
                    query_tokens
                    & tag_tokens
                )
                * 2.0
            )

            scored.append(
                (
                    score,
                    item,
                )
            )

        scored.sort(
            key=lambda pair: pair[0],
            reverse=True,
        )

        return [
            item
            for _, item
            in scored[:limit]
        ]

    def render(
        self,
        query: str,
        limit: int = 5,
    ) -> str:

        results = self.search(
            query=query,
            limit=limit,
        )

        if not results:
            return "(no relevant memory)"

        lines = []

        for item in results:

            tags = ", ".join(
                item.tags
            )

            lines.append(
                f"- [{item.category}] "
                f"{item.content}"
                + (
                    f" (tags: {tags})"
                    if tags
                    else ""
                )
            )

        return "\n".join(lines)