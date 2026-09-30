from dataclasses import dataclass, field
from typing import Literal


TaskStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
]


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: TaskStatus = "pending"

    depends_on: list[str] = field(
        default_factory=list
    )

    result: str | None = None