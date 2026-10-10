from collections.abc import Callable
from dataclasses import asdict
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from devpilot.tasks.model import Task


class TaskStore:

    def __init__(self,path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._lock = RLock()
        if not self.path.exists():
            self._save_unlocked([])

    def all(self) -> list[Task]:
        with self._lock:
            return self._load_unlocked()

    def get(
        self,
        task_id: str,
    ) -> Task | None:
        with self._lock:
            for task in self._load_unlocked():
                if task.id == task_id:
                    return task
        return None

    def create(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
    ) -> Task:

        with self._lock:

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

            tasks = self._load_unlocked()

            tasks.append(task)

            self._save_tasks_unlocked(
                tasks
            )

            return task

    def update(
        self,
        task: Task,
    ) -> None:

        with self._lock:

            tasks = self._load_unlocked()

            found = False

            for index, current in enumerate(
                tasks
            ):

                if current.id == task.id:

                    tasks[index] = task

                    found = True

                    break

            if not found:
                raise ValueError(
                    f"Unknown task: "
                    f"{task.id}"
                )

            self._save_tasks_unlocked(
                tasks
            )

    def mutate(
        self,
        task_id: str,
        mutator: Callable[
            [Task],
            None,
        ],
    ) -> Task:

        with self._lock:

            tasks = self._load_unlocked()

            target: Task | None = None

            for task in tasks:

                if task.id == task_id:
                    target = task
                    break

            if target is None:
                raise ValueError(
                    f"Unknown task: "
                    f"{task_id}"
                )

            mutator(target)

            self._save_tasks_unlocked(
                tasks
            )

            return target

    def _load_unlocked(
        self,
    ) -> list[Task]:

        try:

            data = json.loads(
                self.path.read_text(
                    encoding="utf-8"
                )
            )

        except (
            json.JSONDecodeError,
            OSError,
        ) as error:

            raise RuntimeError(
                f"Failed to load tasks: "
                f"{error}"
            ) from error

        return [
            Task(**item)
            for item in data
        ]

    def _save_tasks_unlocked(
        self,
        tasks: list[Task],
    ) -> None:

        self._save_unlocked(
            [
                asdict(task)
                for task in tasks
            ]
        )

    def _save_unlocked(
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

        temp.replace(
            self.path
        )