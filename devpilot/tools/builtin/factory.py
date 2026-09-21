from pathlib import Path

from devpilot.tools.builtin.bash import (
    create_bash_tool,
)
from devpilot.tools.builtin.edit_file import (
    create_edit_file_tool,
)
from devpilot.tools.builtin.glob_tool import (
    create_glob_tool,
)
from devpilot.tools.builtin.read_file import (
    create_read_file_tool,
)
from devpilot.tools.builtin.write_file import (
    create_write_file_tool,
)
from devpilot.tools.registry import ToolRegistry


def create_builtin_registry(workdir: Path,) -> ToolRegistry:

    registry = ToolRegistry()

    registry.register(create_read_file_tool(workdir))

    registry.register(create_write_file_tool(workdir))

    registry.register(create_edit_file_tool(workdir))

    registry.register(create_glob_tool(workdir))

    registry.register(create_bash_tool(workdir))

    return registry