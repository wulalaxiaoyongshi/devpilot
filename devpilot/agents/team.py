from collections.abc import Callable
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from typing import Any

from devpilot.agents.mailbox import (
    MailboxStore,
)
from devpilot.agents.teammate import (
    Teammate,
)
from devpilot.core.agent import Agent
from devpilot.tasks.graph import TaskGraph
from devpilot.tasks.ownership import (
    TaskOwnership,
)
from devpilot.tasks.store import TaskStore
from devpilot.tools.base import Tool
from devpilot.workspace.worktree import (
    WorktreeManager,
)


AgentFactory = Callable[
    [
        Path,
        str,
        MailboxStore,
    ],
    Agent,
]


class TeamManager:

    def __init__(
        self,
        task_store: TaskStore,
        mailbox: MailboxStore,
        worktrees: WorktreeManager,
        agent_factory: AgentFactory,
        lead_id: str = "lead",
    ):
        self.task_store = task_store
        self.graph = TaskGraph(task_store)
        self.ownership = (
            TaskOwnership(
                store=task_store,
                graph=self.graph,
            )
        )
        self.mailbox = mailbox
        self.worktrees = worktrees
        self.agent_factory = agent_factory
        self.lead_id = lead_id
        self.teammates: dict[
            str,
            Teammate,
        ] = {}

    def add_teammate(
        self,
        agent_id: str,
        role: str,
    ) -> Teammate:

        if agent_id in self.teammates:

            return self.teammates[
                agent_id
            ]

        worktree = (
            self.worktrees.create(
                agent_id
            )
        )

        agent = self.agent_factory(
            worktree.path,
            agent_id,
            self.mailbox,
        )

        teammate = Teammate(
            agent_id=agent_id,
            role=role,
            agent=agent,
            mailbox=self.mailbox,
            worktree=worktree,
            lead_id=self.lead_id,
        )

        self.teammates[
            agent_id
        ] = teammate

        return teammate

    def send(
        self,
        recipient: str,
        content: str,
    ) -> str:

        if (
            recipient
            not in self.teammates
        ):

            return (
                f"Error: unknown teammate "
                f"{recipient}"
            )

        message = self.mailbox.send(
            sender=self.lead_id,
            recipient=recipient,
            content=content,
        )

        return (
            f"Sent {message.id}"
        )

    def run_ready(self) -> str:
        runnable = self.graph.runnable()
        available = [
            teammate
            for teammate
            in self.teammates.values()
            if not teammate.busy
        ]

        if not runnable:
            return (
                "No runnable tasks."
            )

        if not available:
            return (
                "No available teammates."
            )

        count = min(
            len(runnable),
            len(available),
        )

        assignments = list(
            zip(
                runnable[:count],
                available[:count],
            )
        )

        results: list[str] = []

        with ThreadPoolExecutor(
            max_workers=count
        ) as executor:

            futures = {
                executor.submit(
                    self._run_assignment,
                    task.id,
                    teammate.agent_id,
                ):
                (
                    task.id,
                    teammate.agent_id,
                )

                for task, teammate
                in assignments
            }

            for future in as_completed(futures):
                task_id, agent_id = (
                    futures[future]
                )

                try:
                    result = (
                        future.result()
                    )
                except Exception as error:
                    result = (
                        f"{task_id} "
                        f"by {agent_id} "
                        f"failed: {error}"
                    )
                results.append(result)
        return "\n".join(
            results
        )

    def _run_assignment(
        self,
        task_id: str,
        agent_id: str,
    ) -> str:

        teammate = self.teammates[agent_id]
        task = self.ownership.claim(
            task_id=task_id,
            owner=agent_id,
        )

        try:

            result = teammate.run_task(task)
            commit = (
                self.worktrees
                .commit_all(
                    teammate.worktree,
                    message=(
                        f"{task.id}: "
                        f"{task.title}"
                    ),
                )
            )

            final_result = (
                result.output
            )

            if commit is not None:

                final_result += (
                    "\n\nCommit: "
                    f"{commit}"
                )

            self.ownership.complete(
                task_id=task.id,
                owner=agent_id,
                result=final_result,
            )

            return (
                f"{task.id} completed "
                f"by {agent_id} "
                f"on branch "
                f"{teammate.worktree.branch}"
            )

        except Exception as error:

            self.ownership.fail(
                task_id=task.id,
                owner=agent_id,
                error=str(error),
            )

            raise

    def status(
        self,
    ) -> str:

        lines = [
            "Team:"
        ]

        if not self.teammates:
            lines.append(
                "(no teammates)"
            )

        for teammate in (
            self.teammates.values()
        ):

            state = (
                "busy"
                if teammate.busy
                else "idle"
            )

            lines.append(
                f"- {teammate.agent_id} "
                f"[{state}] "
                f"role={teammate.role} "
                f"branch="
                f"{teammate.worktree.branch}"
            )

        lines.append(
            "\nTasks:"
        )

        for task in (
            self.task_store.all()
        ):

            status = (
                self.graph
                .display_status(
                    task
                )
            )

            lines.append(
                f"- {task.id} "
                f"[{status}] "
                f"owner={task.owner} "
                f"{task.title}"
            )

        return "\n".join(
            lines
        )

    def create_tools(
        self,
    ) -> list[Tool]:

        def team_spawn(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            agent_id = arguments.get(
                "agent_id"
            )

            role = arguments.get(
                "role"
            )

            if not isinstance(
                agent_id,
                str,
            ):
                return (
                    "Error: agent_id "
                    "must be string"
                )

            if not isinstance(
                role,
                str,
            ):
                return (
                    "Error: role "
                    "must be string"
                )

            teammate = (
                self.add_teammate(
                    agent_id,
                    role,
                )
            )

            return (
                f"Teammate "
                f"{teammate.agent_id} "
                f"ready on branch "
                f"{teammate.worktree.branch}"
            )

        def team_send(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            recipient = arguments.get(
                "recipient"
            )

            content = arguments.get(
                "content"
            )

            if not isinstance(
                recipient,
                str,
            ):
                return (
                    "Error: recipient "
                    "must be string"
                )

            if not isinstance(
                content,
                str,
            ):
                return (
                    "Error: content "
                    "must be string"
                )

            return self.send(
                recipient,
                content,
            )

        def team_run_ready(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            return self.run_ready()

        def team_status(
            arguments: dict[
                str,
                Any,
            ],
        ) -> str:

            return self.status()

        return [
            Tool(
                name="team_spawn",

                description=(
                    "Create or attach a "
                    "persistent teammate."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "agent_id": {
                            "type": "string"
                        },
                        "role": {
                            "type": "string"
                        },
                    },

                    "required": [
                        "agent_id",
                        "role",
                    ],

                    "additionalProperties": False,
                },

                handler=team_spawn,
            ),

            Tool(
                name="team_send",

                description=(
                    "Send instructions to "
                    "a teammate mailbox."
                ),

                parameters={
                    "type": "object",

                    "properties": {
                        "recipient": {
                            "type": "string"
                        },
                        "content": {
                            "type": "string"
                        },
                    },

                    "required": [
                        "recipient",
                        "content",
                    ],

                    "additionalProperties": False,
                },

                handler=team_send,
            ),

            Tool(
                name="team_run_ready",

                description=(
                    "Assign currently runnable "
                    "tasks to available teammates "
                    "and execute one parallel batch."
                ),

                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },

                handler=team_run_ready,
            ),

            Tool(
                name="team_status",

                description=(
                    "Show team members and "
                    "persistent task status."
                ),

                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },

                handler=team_status,
            ),
        ]