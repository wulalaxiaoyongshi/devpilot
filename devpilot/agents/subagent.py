from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devpilot.core.agent import Agent


@dataclass
class SubagentResult:
    agent_id: str
    output: str


class SubagentRunner:

    def __init__(self,
        agent_factory: Callable[
            [Path, str],
            Agent,
        ],
    ):
        self.agent_factory = agent_factory


    def run(
        self,
        agent_id: str,
        workdir: Path,
        task: str,
    ) -> SubagentResult:

        # 每次都创建全新的 Agent
        agent = self.agent_factory(
            workdir,
            agent_id,
        )

        output = agent.run(
            (
                f"You are subagent "
                f"{agent_id}.\n\n"

                "Complete only the delegated "
                "task below.\n\n"

                f"Task:\n{task}"
            )
        )

        return SubagentResult(
            agent_id=agent_id,
            output=output,
        )