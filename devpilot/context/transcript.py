import json
from pathlib import Path
from uuid import uuid4

from devpilot.core.message import Message


def message_to_dict(
    message: Message,
) -> dict:

    return {
        "role": message.role,
        "content": message.content,

        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        ],

        "tool_call_id": message.tool_call_id,
    }


class TranscriptStore:

    def __init__(
        self,
        root: Path,
    ):
        self.root = root.resolve()

        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

    def write_snapshot(
        self,
        messages: list[Message],
    ) -> Path:

        path = self.root / (
            "transcript_"
            f"{uuid4().hex}.jsonl"
        )

        with path.open(
            "w",
            encoding="utf-8",
        ) as file:

            for message in messages:

                file.write(
                    json.dumps(
                        message_to_dict(
                            message
                        ),
                        ensure_ascii=False,
                    )
                )

                file.write("\n")

        return path

    def write_current(
        self,
        messages: list[Message],
    ) -> Path:

        path = (
            self.root
            / "current.jsonl"
        )

        with path.open(
            "w",
            encoding="utf-8",
        ) as file:

            for message in messages:

                file.write(
                    json.dumps(
                        message_to_dict(
                            message
                        ),
                        ensure_ascii=False,
                    )
                )

                file.write("\n")

        return path