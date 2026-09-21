import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import OpenAI

from devpilot.core.message import Message, ToolCall


@dataclass
class ModelResponse:
    content: str
    tool_calls: list[ToolCall] = field(
        default_factory=list
    )


class Model(Protocol):

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ModelResponse:
        ...


class MockModel:

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ModelResponse:

        last_message = messages[-1]

        return ModelResponse(
            content=f"Mock response to: {last_message.content}"
        )


class DeepSeekModel:

    def __init__(self,model: str = "deepseek-v4-flash",):
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")

        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is not set"
            )

        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ModelResponse:

        api_messages = [
            self._to_api_message(message)
            for message in messages
        ]
        if system:
            api_messages.insert(0,
                {
                    "role": "system",
                    "content": system,
                },
            )
        request = {
            "model": self.model,
            "messages": api_messages,
            "extra_body": {
                "thinking": {
                    "type": "disabled"
                }
            },
        }

        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        response = self.client.chat.completions.create(
            **request
        )

        message = response.choices[0].message

        tool_calls = []

        for tool_call in message.tool_calls or []:

            try:
                arguments = json.loads(
                    tool_call.function.arguments
                )

            except json.JSONDecodeError as error:
                raise ValueError(
                    "Model returned invalid tool arguments"
                ) from error

            tool_calls.append(
                ToolCall(
                    id=tool_call.id,
                    name=tool_call.function.name,
                    arguments=arguments,
                )
            )

        return ModelResponse(
            content=message.content or "",
            tool_calls=tool_calls,
        )

    @staticmethod
    def _to_api_message(
        message: Message,
    ) -> dict[str, Any]:

        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }

        result: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }

        if message.tool_calls:

            result["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(
                            tool_call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]

        return result