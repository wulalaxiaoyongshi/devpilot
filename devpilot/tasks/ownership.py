from datetime import datetime, timezone
from devpilot.tasks.graph import TaskGraph
from devpilot.tasks.model import Task
from devpilot.tasks.store import TaskStore


class TaskOwnership:

    def __init__(self,store: TaskStore,graph: TaskGraph,):
        self.store = store
        self.graph = graph

    def claim(self,task_id: str,owner: str,) -> Task:

        def mutate(task: Task) -> None:
            if task.status != "pending":
                raise ValueError(
                    f"Task {task.id} "
                    f"is not pending"
                )
            if task.owner is not None:
                raise ValueError(
                    f"Task {task.id} "
                    f"is already owned by "
                    f"{task.owner}"
                )
            if self.graph.is_blocked(task):
                raise ValueError(
                    f"Task {task.id} "
                    f"is blocked"
                )

            task.owner = owner
            task.status = (
                "in_progress"
            )
            task.attempts += 1
            task.claimed_at = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )
        return self.store.mutate(
            task_id,
            mutate,
        )

    def complete(
        self,
        task_id: str,
        owner: str,
        result: str,
    ) -> Task:

        def mutate(
            task: Task,
        ) -> None:

            self._require_owner(
                task,
                owner,
            )

            if (
                task.status
                != "in_progress"
            ):
                raise ValueError(
                    "Only in-progress "
                    "tasks can complete"
                )

            task.status = "completed"
            task.result = result

        return self.store.mutate(
            task_id,
            mutate,
        )

    def fail(
        self,
        task_id: str,
        owner: str,
        error: str,
    ) -> Task:

        def mutate(
            task: Task,
        ) -> None:

            self._require_owner(
                task,
                owner,
            )

            task.status = "failed"
            task.result = error

        return self.store.mutate(
            task_id,
            mutate,
        )

    @staticmethod
    def _require_owner(
        task: Task,
        owner: str,
    ) -> None:

        if task.owner != owner:

            raise ValueError(
                f"Task {task.id} "
                f"is owned by "
                f"{task.owner}, "
                f"not {owner}"
            )