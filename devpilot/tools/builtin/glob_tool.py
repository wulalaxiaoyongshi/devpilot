import glob
from pathlib import Path
from typing import Any

from devpilot.tools.base import Tool


def create_glob_tool(workdir: Path,) -> Tool:

    root = workdir.resolve()

    def handler(arguments: dict[str, Any],) -> str:

        pattern = arguments.get("pattern")

        if not isinstance(pattern,str,):
            return (
                "Error: pattern must "
                "be string"
            )

        matches = glob.glob( pattern,root_dir=root,recursive=True,)
        safe_matches = []
        for match in matches:
            target = (root / match).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            safe_matches.append(match)

        if not safe_matches:
            return "(no matches)"

        return "\n".join(
            safe_matches[:200]
        )

    return Tool(
        name="glob",

        description=(
            "Find files matching "
            "a glob pattern."
        ),

        parameters={
            "type": "object",

            "properties": {
                "pattern": {
                    "type": "string"
                }
            },

            "required": [
                "pattern"
            ],

            "additionalProperties": False,
        },

        handler=handler,
    )