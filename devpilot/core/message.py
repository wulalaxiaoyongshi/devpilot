from dataclasses import dataclass
from typing import Literal

Role=Literal["user","assistant"]

@dataclass
class Message:
    role:Role
    content:str