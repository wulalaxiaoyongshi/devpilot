from pathlib import Path
from typing import Any
from devpilot.tools.base import Tool


def create_edit_file_tool(workdir: Path,) -> Tool:

    root = workdir.resolve()

    def handler(arguments: dict[str, Any],) -> str:
        path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not all(
            isinstance(value, str)
            for value in (path,old_text,new_text,)
        ):
            return (
                "Error: all arguments "
                "must be strings"
            )
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return (
                "Error: path outside "
                "workspace"
            )
        if not target.is_file():
            return (
                f"Error: file not found: "
                f"{path}"
            )
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            return (
                "Error: expected exactly "
                f"1 occurrence, found "
                f"{count}"
            )
        updated = content.replace(old_text,new_text,1,)

        target.write_text(updated,encoding="utf-8",)
        return (f"Edited {path}")

    return Tool(
        name="edit_file",

        description=(
            "Replace one exact text "
            "fragment inside a file."
        ),

        parameters={
            "type": "object",

            "properties": {

                "path": {
                    "type": "string"
                },

                "old_text": {
                    "type": "string"
                },

                "new_text": {
                    "type": "string"
                },
            },

            "required": [
                "path",
                "old_text",
                "new_text",
            ],

            "additionalProperties": False,
        },

        handler=handler,
    )