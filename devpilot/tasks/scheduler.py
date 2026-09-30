from typing import Any

from devpilot.tasks.graph import (
    TaskGraph,
)
from devpilot.tasks.store import (
    TaskStore,
)
from devpilot.tools.base import Tool


VALID_TASK_STATUSES = {
    "pending",
    "in_progress",
    "completed",
    "failed",
}


class TaskScheduler:

    def __init__(
        self,
        store: TaskStore,
    ):
        self.store = store
        self.graph = TaskGraph(store)

    def create(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
    ) -> str:

        dependencies = (
            depends_on or []
        )

        self.graph.validate_dependencies(
            dependencies
        )

        task = self.store.create(
            title=title,
            description=description,
            depends_on=dependencies,
        )

        try:
            self.graph.validate_no_cycles()

        except Exception:
            raise

        return (
            f"Created {task.id}: "
            f"{task.title}"
        )

    def update(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
    ) -> str:

        if status not in (
            VALID_TASK_STATUSES
        ):
            return (
                f"Error: invalid status "
                f"'{status}'"
            )

        task = self.store.get(
            task_id
        )

        if task is None:
            return (
                f"Error: unknown task "
                f"'{task_id}'"
            )

        if (
            status == "in_progress"
            and self.graph.is_blocked(
                task
            )
        ):
            return (
                "Error: task is blocked "
                "by unfinished dependencies"
            )

        if (
            status == "completed"
            and self.graph.is_blocked(
                task
            )
        ):
            return (
                "Error: cannot complete "
                "a blocked task"
            )

        task.status = status

        if result is not None:
            task.result = result

        self.store.update(
            task
        )

        return (
            f"Updated {task.id} "
            f"to {status}"
        )

    def render(
        self,
    ) -> str:

        tasks = self.store.all()

        if not tasks:
            return "(no persistent tasks)"

        lines = []

        for task in tasks:

            status = (
                self.graph
                .display_status(
                    task
                )
            )

            dependency_text = ""

            if task.depends_on:

                dependency_text = (
                    " depends_on="
                    + ",".join(
                        task.depends_on
                    )
                )

            lines.append(
                f"- {task.id} "
                f"[{status}] "
                f"{task.title}"
                f"{dependency_text}"
            )

        runnable = (
            self.graph.runnable()
        )

        if runnable:

            lines.append(
                "\nRunnable now:"
            )

            for task in runnable:

                lines.append(
                    f"- {task.id}: "
                    f"{task.title}"
                )

        return "\n".join(lines)

    def create_tools(
        self,
    ) -> list[Tool]:

        def task_create(arguments: dict[str,Any]) -> str:

            title = arguments.get(
                "title"
            )

            description = (
                arguments.get(
                    "description",
                    "",
                )
            )

            depends_on = (
                arguments.get(
                    "depends_on",
                    [],
                )
            )

            if not isinstance(
                title,
                str,
            ):
                return (
                    "Error: title must "
                    "be string"
                )

            if not isinstance(
                description,
                str,
            ):
                return (
                    "Error: description "
                    "must be string"
                )

            if not isinstance(
                depends_on,
                list,
            ):
                return (
                    "Error: depends_on "
                    "must be list"
                )

            clean_dependencies = [
                dependency
                for dependency in depends_on
                if isinstance(
                    dependency,
                    str,
                )
            ]

            try:
                return self.create(
                    title=title,
                    description=description,
                    depends_on=(
                        clean_dependencies
                    ),
                )

            except ValueError as error:
                return (
                    f"Error: {error}"
                )

        def task_update(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            task_id = arguments.get(
                "task_id"
            )

            status = arguments.get(
                "status"
            )

            result = arguments.get(
                "result"
            )

            if not isinstance(
                task_id,
                str,
            ):
                return (
                    "Error: task_id must "
                    "be string"
                )

            if not isinstance(
                status,
                str,
            ):
                return (
                    "Error: status must "
                    "be string"
                )

            if (
                result is not None
                and not isinstance(
                    result,
                    str,
                )
            ):
                return (
                    "Error: result must "
                    "be string"
                )

            return self.update(
                task_id=task_id,
                status=status,
                result=result,
            )

        def task_list(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            return self.render()

        return [
            Tool(
                name="task_create",

                description=(
                    "Create a persistent task "
                    "with optional dependencies."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "title": {
                            "type": "string"
                        },

                        "description": {
                            "type": "string"
                        },

                        "depends_on": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                        },
                    },

                    "required": [
                        "title"
                    ],

                    "additionalProperties": False,
                },

                handler=task_create,
            ),

            Tool(
                name="task_update",

                description=(
                    "Update the status or "
                    "result of a persistent task."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "task_id": {
                            "type": "string"
                        },

                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "completed",
                                "failed",
                            ],
                        },

                        "result": {
                            "type": "string"
                        },
                    },

                    "required": [
                        "task_id",
                        "status",
                    ],

                    "additionalProperties": False,
                },

                handler=task_update,
            ),

            Tool(
                name="task_list",

                description=(
                    "Show the persistent task "
                    "graph and currently "
                    "runnable tasks."
                ),

                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },

                handler=task_list,
            ),
        ]