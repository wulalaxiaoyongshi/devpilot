from dataclasses import dataclass
from typing import Any

from devpilot.tools.base import Tool


VALID_STATUSES = {
    "pending",
    "in_progress",
    "completed",
}


@dataclass
class TodoItem:
    content: str
    status: str


class TodoManager:

    def __init__(self):
        self.items: list[TodoItem] = []

    def replace(
        self,
        raw_items: list[dict[str, Any]],
    ) -> str:

        new_items: list[TodoItem] = []

        for raw in raw_items:

            content = raw.get("content")
            status = raw.get("status")

            if not isinstance(content, str):
                return (
                    "Error: todo content "
                    "must be a string"
                )

            if status not in VALID_STATUSES:
                return (
                    "Error: todo status must be "
                    "pending, in_progress, "
                    "or completed"
                )

            new_items.append(
                TodoItem(
                    content=content,
                    status=status,
                )
            )

        self.items = new_items

        return self.render()

    def render(self) -> str:

        if not self.items:
            return "(no todos)"

        lines = []

        for index, item in enumerate(
            self.items,
            start=1,
        ):

            lines.append(
                f"{index}. "
                f"[{item.status}] "
                f"{item.content}"
            )

        return "\n".join(lines)


def create_todo_tool(
    manager: TodoManager,
) -> Tool:

    def handler(
        arguments: dict[str, Any],
    ) -> str:

        items = arguments.get("items")

        if not isinstance(items, list):
            return (
                "Error: items must "
                "be a list"
            )

        return manager.replace(items)

    return Tool(
        name="todo_write",

        description=(
            "Replace the current task plan. "
            "Use this for multi-step work "
            "to track pending, in-progress, "
            "and completed steps."
        ),

        parameters={
            "type": "object",

            "properties": {
                "items": {
                    "type": "array",

                    "items": {
                        "type": "object",

                        "properties": {

                            "content": {
                                "type": "string"
                            },

                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                ],
                            },
                        },

                        "required": [
                            "content",
                            "status",
                        ],

                        "additionalProperties": False,
                    },
                }
            },

            "required": [
                "items"
            ],

            "additionalProperties": False,
        },

        handler=handler,
    )