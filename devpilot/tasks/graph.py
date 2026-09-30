from devpilot.tasks.model import Task
from devpilot.tasks.store import TaskStore


class TaskGraph:

    def __init__(self,store: TaskStore):
        self.store = store

    def validate_dependencies(self,depends_on: list[str]) -> None:
        for task_id in depends_on:
            if self.store.get(task_id) is None:
                raise ValueError(
                    "Unknown dependency: "
                    f"{task_id}"
                )

    def is_blocked(self,task: Task) -> bool:
        for dependency_id in (task.depends_on):
            dependency = self.store.get(dependency_id)
            if dependency is None:
                return True
            if dependency.status!= "completed":
                return True
        return False

    def runnable(self) -> list[Task]:
        runnable_tasks = []
        for task in self.store.all():
            if task.status != "pending":
                continue
            if self.is_blocked(task):
                continue
            runnable_tasks.append(task)
        return runnable_tasks

    def validate_no_cycles(self) -> None:
        tasks = {
            task.id: task
            for task in self.store.all()
        }

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:

            if task_id in visited:
                return

            if task_id in visiting:
                raise ValueError(
                    "Task dependency cycle "
                    f"detected at {task_id}"
                )

            visiting.add(task_id)
            task = tasks.get(task_id)
            if task is not None:
                for dependency_id in task.depends_on:
                    visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)


    def display_status(
        self,
        task: Task,
    ) -> str:

        if (
            task.status == "pending"
            and self.is_blocked(task)
        ):
            return "blocked"

        return task.status