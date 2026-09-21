from pathlib import Path
from typing import Any

from devpilot.tools.base import Tool


def create_write_file_tool(workdir: Path,) -> Tool:

    root = workdir.resolve()
    def handler(arguments: dict[str, Any],) -> str:
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str):
            return (
                "Error: path must "
                "be string"
            )
        if not isinstance(content,str,):
            return (
                "Error: content must "
                "be string"
            )
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return (
                "Error: path outside "
                "workspace"
            )

        target.parent.mkdir(parents=True,exist_ok=True,)
        target.write_text(content,encoding="utf-8",)
        return (
            f"Wrote {len(content)} "
            f"characters to {path}"
        )

    return Tool(
        name="write_file",
        description=(
            "Write a UTF-8 text file "
            "inside the workspace."
        ),
        parameters={
            "type": "object",

            "properties": {

                "path": {
                    "type": "string"
                },

                "content": {
                    "type": "string"
                },
            },

            "required": [
                "path",
                "content",
            ],

            "additionalProperties": False,
        },
        handler=handler,
    )