from devpilot.context.manager import (
    ContextManager,
)
from devpilot.core.message import Message
from devpilot.core.model import Model
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
        max_steps: int = 200,
    ):
        self.model = model
        self.tools = tools
        self.hooks = hooks
        self.context = context

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

    def run(self,user_input: str,) -> str:

        self.messages.append(
            Message(
                role="user",
                content=user_input,
            )
        )

        for _ in range(self.max_steps):

            # --------------------
            # Context Runtime
            # --------------------

            prepared = (
                self.context.prepare(
                    self.messages
                )
            )

            self.messages[:] = prepared

            system = (
                self.context
                .build_system_prompt()
            )

            # --------------------
            # Model
            # --------------------

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

            # --------------------
            # Save Assistant
            # --------------------

            self.messages.append(
                Message(
                    role="assistant",
                    content=(
                        response.content
                    ),
                    tool_calls=(
                        response.tool_calls
                    ),
                )
            )

            # --------------------
            # Stop
            # --------------------

            if not response.tool_calls:

                self.context.record(
                    self.messages
                )

                return response.content

            # --------------------
            # Tools
            # --------------------

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

        self.context.record(
            self.messages
        )

        raise RuntimeError(
            "Agent exceeded "
            f"{self.max_steps} steps"
        )