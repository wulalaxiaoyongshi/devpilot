from devpilot.context.manager import (
    ContextManager,
)
from devpilot.core.message import Message
from devpilot.core.model import Model
from devpilot.memory.manager import (
    MemoryManager,
)
from devpilot.tasks.scheduler import (
    TaskScheduler,
)
from devpilot.tools.dispatcher import (
    ToolDispatcher,
)
from devpilot.tools.hooks import (
    HookManager,
)
from devpilot.tools.registry import (
    ToolRegistry,
)


class Agent:

    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        hooks: HookManager,
        context: ContextManager,
        memory: MemoryManager,
        tasks: TaskScheduler,
        max_steps: int = 20,
    ):
        self.model = model
        self.tools = tools
        self.hooks = hooks
        self.context = context
        self.memory = memory
        self.tasks = tasks

        self.max_steps = max_steps

        self.dispatcher = (
            ToolDispatcher(
                registry=tools,
                hooks=hooks,
            )
        )

        self.messages: list[
            Message
        ] = []

    def run(self,user_input: str) -> str:
        self.messages.append(
            Message(
                role="user",
                content=user_input,
            )
        )
        for _ in range(
            self.max_steps
        ):

            # =========================
            # Active Context
            # =========================
            prepared = (
                self.context.prepare(
                    self.messages
                )
            )

            self.messages[:] = (
                prepared
            )

            # =========================
            # Persistent Memory
            # =========================

            memory_context = (
                self.memory.recall(
                    user_input
                )
            )

            # =========================
            # Persistent Task Graph
            # =========================

            task_context = (
                self.tasks.render()
            )

            # =========================
            # Build System Prompt
            # =========================

            system = (
                self.context
                .build_system_prompt(
                    extra_sections=[
                        (
                            "## Relevant "
                            "Persistent Memory\n"
                            f"{memory_context}"
                        ),

                        (
                            "## Persistent "
                            "Task Graph\n"
                            f"{task_context}"
                        ),
                    ]
                )
            )

            # =========================
            # Model
            # =========================

            response = (
                self.model.complete(
                    messages=(
                        self.messages
                    ),
                    tools=(
                        self.tools.schemas()
                    ),
                    system=system,
                )
            )

            # =========================
            # Assistant Message
            # =========================

            self.messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=(
                        response.tool_calls
                    ),
                )
            )

            # =========================
            # Stop
            # =========================

            if not response.tool_calls:

                self.memory.capture_turn(
                    user_input=(
                        user_input
                    ),
                    assistant_output=(
                        response.content
                    ),
                )

                return response.content

            # =========================
            # Tool Calls
            # =========================

            for tool_call in (
                response.tool_calls
            ):

                result = (
                    self.dispatcher
                    .execute(
                        tool_call
                    )
                )

                self.messages.append(
                    Message(
                        role="tool",
                        content=result,
                        tool_call_id=(
                            tool_call.id
                        ),
                    )
                )

        raise RuntimeError(
            "Agent exceeded "
            f"{self.max_steps} steps"
        )