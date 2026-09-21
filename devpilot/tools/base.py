from dataclasses import dataclass
from typing import Any, Callable

ToolHandler=Callable[[dict[str, Any]], str]

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function":{
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }