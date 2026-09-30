import subprocess
from pathlib import Path
from typing import Any

from devpilot.tools.base import Tool


def create_bash_tool(workdir: Path,) -> Tool:

    root = workdir.resolve()

    def handler(arguments: dict[str, Any],) -> str:

        command = arguments.get("command")

        if not isinstance(command,str,):
            return (
                "Error: command must "
                "be string"
            )

        result = subprocess.run(
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )

        output = (result.stdout+ result.stderr).strip()
        # 防止超大结果撑爆 Context
        if len(output) > 30000:
            output = output[-30000:]

        return (
            f"exit_code="
            f"{result.returncode}\n"
            f"{output}"
        )

    return Tool(
        name="bash",

        description=(
            "Run a shell command "
            "inside the workspace."
        ),

        parameters={
            "type": "object",

            "properties": {
                "command": {
                    "type": "string"
                }
            },

            "required": [
                "command"
            ],

            "additionalProperties": False,
        },

        handler=handler,
    )