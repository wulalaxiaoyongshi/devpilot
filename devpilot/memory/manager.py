from typing import Any

from devpilot.core.model import Model
from devpilot.memory.consolidate import (
    MemoryConsolidator,
)
from devpilot.memory.extractor import (
    MemoryExtractor,
)
from devpilot.memory.recall import (
    MemoryRecall,
)
from devpilot.memory.store import (
    MemoryStore,
)
from devpilot.tools.base import Tool


class MemoryManager:

    def __init__(
        self,
        store: MemoryStore,
        model: Model,
        auto_extract: bool = True,
    ):
        self.store = store
        self.recall_engine = MemoryRecall(store)
        self.consolidator = MemoryConsolidator(store)
        self.extractor =  MemoryExtractor(model)
        self.auto_extract = auto_extract


    def remember(
        self,
        content: str,
        category: str,
        tags: list[str] | None = None,
        source: str = "agent",
    ) -> str:

        item = (
            self.consolidator.save(
                content=content,
                category=category,
                tags=tags,
                source=source,
            )
        )

        return (
            f"Memory saved: "
            f"{item.id}"
        )

    def recall(
        self,
        query: str,
        limit: int = 5,
    ) -> str:

        return (
            self.recall_engine.render(
                query=query,
                limit=limit,
            )
        )

    def capture_turn(
        self,
        user_input: str,
        assistant_output: str,
    ) -> None:

        if not self.auto_extract:
            return

        memories = (
            self.extractor.extract(
                user_input=user_input,
                assistant_output=(
                    assistant_output
                ),
            )
        )

        for memory in memories:

            self.consolidator.save(
                content=memory.content,
                category=memory.category,
                tags=memory.tags,
                source="auto_extract",
            )

    def create_tools(
        self,
    ) -> list[Tool]:

        def memory_write(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            content = arguments.get(
                "content"
            )

            category = arguments.get(
                "category"
            )

            tags = arguments.get(
                "tags",
                [],
            )

            if not isinstance(
                content,
                str,
            ):
                return (
                    "Error: content must "
                    "be string"
                )

            if not isinstance(
                category,
                str,
            ):
                return (
                    "Error: category must "
                    "be string"
                )

            if not isinstance(
                tags,
                list,
            ):
                return (
                    "Error: tags must "
                    "be list"
                )

            clean_tags = [
                tag
                for tag in tags
                if isinstance(
                    tag,
                    str,
                )
            ]

            return self.remember(
                content=content,
                category=category,
                tags=clean_tags,
            )

        def memory_search(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            query = arguments.get(
                "query"
            )

            if not isinstance(
                query,
                str,
            ):
                return (
                    "Error: query must "
                    "be string"
                )

            return self.recall(
                query
            )

        return [
            Tool(
                name="memory_write",

                description=(
                    "Store durable project "
                    "information for future "
                    "sessions."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "content": {
                            "type": "string"
                        },

                        "category": {
                            "type": "string",
                            "enum": [
                                "project_fact",
                                "decision",
                                "constraint",
                                "preference",
                                "lesson",
                            ],
                        },

                        "tags": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                        },
                    },

                    "required": [
                        "content",
                        "category",
                    ],

                    "additionalProperties": False,
                },

                handler=memory_write,
            ),

            Tool(
                name="memory_search",

                description=(
                    "Search persistent "
                    "project memory."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "query": {
                            "type": "string"
                        }
                    },

                    "required": [
                        "query"
                    ],

                    "additionalProperties": False,
                },

                handler=memory_search,
            ),
        ]