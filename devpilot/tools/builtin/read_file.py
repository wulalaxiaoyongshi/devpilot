from pathlib import Path
from typing import Any

from devpilot.tools.base import Tool


def create_read_file_tool(workdir: Path,) -> Tool:

    root = workdir.resolve()
    def handler(arguments: dict[str, Any],) -> str:
        path = arguments.get("path")
        if not isinstance(path, str):
            return (
                "Error: path must "
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
        if not target.is_file():
            return (
                f"Error: file not found: "
                f"{path}"
            )
        return target.read_text(
            encoding="utf-8",
            errors="replace",
        )
    return Tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file "
            "inside the workspace."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string"
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
    )