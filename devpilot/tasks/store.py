from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from devpilot.tasks.model import Task


class TaskStore:

    def __init__(self,path: Path,):
        self.path = path.resolve()
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        if not self.path.exists():
            self._save([])

    def all(self) -> list[Task]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            Task(**item)
            for item in data
        ]

    def get(self,task_id: str) -> Task | None:

        for task in self.all():
            if task.id == task_id:
                return task
        return None

    def create(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
    ) -> Task:

        task = Task(
            id=(
                "task_"
                + uuid4().hex[:10]
            ),
            title=title,
            description=description,
            depends_on=(
                depends_on or []
            ),
        )

        tasks = self.all()

        tasks.append(task)

        self._save_tasks(tasks)

        return task

    def update(
        self,
        task: Task,
    ) -> None:

        tasks = self.all()

        updated = []

        found = False

        for current in tasks:

            if current.id == task.id:
                updated.append(task)
                found = True
            else:
                updated.append(current)

        if not found:
            raise ValueError(
                f"Unknown task: "
                f"{task.id}"
            )

        self._save_tasks(updated)

    def _save_tasks(
        self,
        tasks: list[Task],
    ) -> None:

        self._save(
            [
                asdict(task)
                for task in tasks
            ]
        )

    def _save(
        self,
        data: list[dict],
    ) -> None:

        temp = self.path.with_suffix(
            ".tmp"
        )

        temp.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp.replace(self.path)