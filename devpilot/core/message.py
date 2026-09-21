from dataclasses import dataclass, field
from typing import Any, Literal

Role=Literal["assistant","user","tool"]

@dataclass
class ToolCall:
    id:str
    name:str
    arguments:dict[str, Any]

@dataclass
class Message:
    role:Role
    content:str=""
    tool_calls:list[ToolCall]=field(default_factory=list)
    tool_call_id:str | None=None

