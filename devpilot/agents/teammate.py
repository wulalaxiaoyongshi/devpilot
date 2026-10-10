from dataclasses import dataclass

from devpilot.agents.mailbox import (
    MailboxStore,
)
from devpilot.core.agent import Agent
from devpilot.tasks.model import Task
from devpilot.workspace.worktree import (
    WorktreeRef,
)


@dataclass
class TeammateResult:
    agent_id: str
    task_id: str
    output: str


class Teammate:

    def __init__(
        self,
        agent_id: str,
        role: str,
        agent: Agent,
        mailbox: MailboxStore,
        worktree: WorktreeRef,
        lead_id: str = "lead",
    ):
        self.agent_id = agent_id
        self.role = role
        self.agent = agent
        self.mailbox = mailbox
        self.worktree = worktree
        self.lead_id = lead_id
        self.busy = False

    def run_task(self,task: Task) -> TeammateResult:
        if self.busy:
            raise RuntimeError(
                f"{self.agent_id} "
                "is already busy"
            )
        self.busy = True
        try:
            inbox = self.mailbox.render_inbox(self.agent_id)


            prompt = (
                f"You are teammate "
                f"{self.agent_id}.\n"

                f"Role: {self.role}\n\n"

                "You work only inside your "
                "assigned Git worktree.\n"

                "Inspect real files and run "
                "relevant tests before claiming "
                "success.\n\n"

                "## Inbox\n"
                f"{inbox}\n\n"

                "## Assigned Task\n"
                f"ID: {task.id}\n"
                f"Title: {task.title}\n"
                f"Description: "
                f"{task.description}"
            )

            output = self.agent.run(prompt)

            self.mailbox.send(
                sender=self.agent_id,
                recipient=self.lead_id,
                content=(
                    f"Task {task.id} "
                    f"finished.\n\n"
                    f"{output}"
                ),
            )

            return TeammateResult(
                agent_id=self.agent_id,
                task_id=task.id,
                output=output,
            )

        finally:

            self.busy = False