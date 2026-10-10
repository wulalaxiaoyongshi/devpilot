from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from devpilot.tools.base import Tool


@dataclass
class MailMessage:
    id: str
    sender: str
    recipient: str
    content: str
    created_at: str
    read_at: str | None = None


class MailboxStore:

    def __init__(
        self,
        path: Path,
    ):
        self.path = path.resolve()

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._lock = RLock()

        if not self.path.exists():
            self._save_unlocked([])

    def send(
        self,
        sender: str,
        recipient: str,
        content: str,
    ) -> MailMessage:

        with self._lock:

            message = MailMessage(
                id=(
                    "mail_"
                    + uuid4().hex[:12]
                ),
                sender=sender,
                recipient=recipient,
                content=content,
                created_at=(
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            )

            messages = (
                self._load_unlocked()
            )

            messages.append(
                message
            )

            self._save_messages_unlocked(
                messages
            )

            return message

    def read(
        self,
        recipient: str,
        unread_only: bool = True,
        mark_read: bool = True,
    ) -> list[MailMessage]:

        with self._lock:

            messages = (
                self._load_unlocked()
            )

            selected = []

            changed = False

            for message in messages:

                if (
                    message.recipient
                    != recipient
                ):
                    continue

                if (
                    unread_only
                    and message.read_at
                    is not None
                ):
                    continue

                selected.append(
                    message
                )

                if (
                    mark_read
                    and message.read_at
                    is None
                ):

                    message.read_at = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    changed = True

            if changed:

                self._save_messages_unlocked(
                    messages
                )

            return selected

    def render_inbox(
        self,
        recipient: str,
    ) -> str:

        messages = self.read(
            recipient=recipient,
            unread_only=True,
            mark_read=True,
        )

        if not messages:
            return "(no new messages)"

        lines = []

        for message in messages:

            lines.append(
                f"From {message.sender}: "
                f"{message.content}"
            )

        return "\n".join(
            lines
        )

    def _load_unlocked(
        self,
    ) -> list[MailMessage]:

        data = json.loads(
            self.path.read_text(
                encoding="utf-8"
            )
        )

        return [
            MailMessage(**item)
            for item in data
        ]

    def _save_messages_unlocked(
        self,
        messages: list[
            MailMessage
        ],
    ) -> None:

        self._save_unlocked(
            [
                asdict(message)
                for message in messages
            ]
        )

    def _save_unlocked(
        self,
        data: list[dict],
    ) -> None:

        temp = self.path.with_suffix(
            ".tmp"
        )

        temp.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp.replace(
            self.path
        )


def create_mailbox_tools(
    mailbox: MailboxStore,
    agent_id: str,
) -> list[Tool]:

    def mail_send(
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

        message = mailbox.send(
            sender=agent_id,
            recipient=recipient,
            content=content,
        )

        return (
            f"Message sent: "
            f"{message.id}"
        )

    def mail_read(
        arguments: dict[
            str,
            Any,
        ],
    ) -> str:

        return mailbox.render_inbox(
            agent_id
        )

    return [
        Tool(
            name="mail_send",

            description=(
                "Send a message to "
                "another teammate or lead."
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

            handler=mail_send,
        ),

        Tool(
            name="mail_read",

            description=(
                "Read unread messages "
                "sent to this agent."
            ),

            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },

            handler=mail_read,
        ),
    ]