from devpilot.tools.base import Tool


class ToolRegistry:

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self,tool: Tool,) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"Tool already registered: "
                f"{tool.name}"
            )
        self._tools[tool.name] = tool


    def get(self,name: str,) -> Tool | None:
        return self._tools.get(name)


    def schemas(self ) -> list[dict]:
        return [
            tool.schema()
            for tool in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())