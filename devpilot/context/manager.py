from pathlib import Path

from devpilot.context.compact import (
    ContextCompactor,
    ContextConfig,
)
from devpilot.context.skills import (
    SkillCatalog,
)
from devpilot.context.todo import (
    TodoManager,
)
from devpilot.context.transcript import (
    TranscriptStore,
)
from devpilot.core.message import Message
from devpilot.core.model import Model


class ContextManager:

    def __init__(
        self,
        model: Model,
        workspace: Path,
        todos: TodoManager,
        skills: SkillCatalog,
        base_system: str | None = None,
        config: ContextConfig | None = None,
    ):
        self.workspace = (
            workspace.resolve()
        )

        self.todos = todos
        self.skills = skills

        self.base_system = (
            base_system
            or (
                "You are DevPilot, "
                "a software engineering agent. "
                "Use tools to inspect real state "
                "instead of guessing. "
                "Do not claim commands succeeded "
                "unless tool output confirms it."
            )
        )

        transcript_root = (
            self.workspace
            / ".devpilot"
            / "transcripts"
        )

        self.transcripts = (
            TranscriptStore(
                transcript_root
            )
        )

        self.compactor = (
            ContextCompactor(
                model=model,
                workspace=self.workspace,
                transcripts=self.transcripts,
                config=config,
            )
        )

    def prepare(
        self,
        messages: list[Message],
    ) -> list[Message]:

        return self.compactor.prepare(
            messages
        )


    def build_system_prompt(
            self,
            extra_sections: list[str] | None = None,
    ) -> str:
        sections = [
            self.base_system,

            (
                "## Current Todo State\n"
                f"{self.todos.render()}"
            ),

            (
                "## Available Skills\n"
                f"{self.skills.catalog_text()}"
            ),

            (
                "For complex multi-step work, "
                "use todo_write to keep the "
                "current execution plan accurate.\n"
                "Use load_skill when a listed "
                "skill is relevant."
            ),
        ]

        if extra_sections:
            sections.extend(
                section
                for section in extra_sections
                if section
            )

        return "\n\n".join(
            sections
        )

    def record(
        self,
        messages: list[Message],
    ) -> None:

        self.transcripts.write_current(
            messages
        )