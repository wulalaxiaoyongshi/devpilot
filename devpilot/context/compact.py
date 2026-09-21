import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

from devpilot.core.message import Message
from devpilot.core.model import Model
from devpilot.context.transcript import (
    TranscriptStore,
    message_to_dict,
)


@dataclass(frozen=True)
class ContextConfig:

    context_char_limit: int = 50000

    large_tool_result_limit: int = 30000

    old_tool_result_limit: int = 1000

    keep_recent_tool_results: int = 3

    keep_recent_messages: int = 6

    summary_input_limit: int = 80000

    tool_preview_chars: int = 2000


class ContextCompactor:

    def __init__(
        self,
        model: Model,
        workspace: Path,
        transcripts: TranscriptStore,
        config: ContextConfig | None = None,
    ):
        self.model = model

        self.workspace = (
            workspace.resolve()
        )

        self.transcripts = transcripts

        self.config = (
            config or ContextConfig()
        )

        self.tool_output_dir = (
            self.workspace
            / ".devpilot"
            / "tool_outputs"
        )

        self.tool_output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    def estimate_chars(
        self,
        messages: list[Message],
    ) -> int:

        return sum(
            len(
                json.dumps(
                    message_to_dict(
                        message
                    ),
                    ensure_ascii=False,
                )
            )
            for message in messages
        )

    def prepare(
        self,
        messages: list[Message],
    ) -> list[Message]:
        original_messages=messages
        prepared = copy.deepcopy(messages)
        # 第一级防御：检查并把超过 30,000 字符的超大工具输出卸载到磁盘
        self._offload_large_tool_results(prepared)
        # 第二级防御：将非最近 3 次、且长度大于 1,000 字符的旧工具结果削减为占位符
        self._trim_old_tool_results(prepared)
        # 第三级防御：如果经过前两轮瘦身后，总字符依然超出 50,000，触发大模型历史归纳
        if self.estimate_chars(prepared)> self.config.context_char_limit:
            prepared = self._compact_history(original_messages,prepared)
        return prepared

    def _offload_large_tool_results(
        self,
        messages: list[Message],
    ) -> None:

        for message in messages:

            if message.role != "tool":
                continue

            if (
                len(message.content)
                <= self.config.large_tool_result_limit
            ):
                continue

            safe_id = re.sub(
                r"[^A-Za-z0-9_.-]",
                "_",
                message.tool_call_id
                or "unknown",
            )

            path = (
                self.tool_output_dir
                / f"{safe_id}.txt"
            )

            path.write_text(
                message.content,
                encoding="utf-8",
            )

            preview = message.content[
                : self.config.tool_preview_chars
            ]

            message.content = (
                "[Large tool output stored at "
                f"{path}]\n\n"
                "[Preview]\n"
                f"{preview}"
            )

    def _trim_old_tool_results(
        self,
        messages: list[Message],
    ) -> None:

        tool_positions = [
            index
            for index, message
            in enumerate(messages)
            if message.role == "tool"
        ]

        protected = set(
            tool_positions[
                -self.config.keep_recent_tool_results:
            ]
        )

        for index in tool_positions:

            if index in protected:
                continue

            message = messages[index]

            if (
                len(message.content)
                <= self.config.old_tool_result_limit
            ):
                continue

            message.content = (
                "[Old tool result omitted "
                "from active context. "
                f"tool_call_id="
                f"{message.tool_call_id}]"
            )

    def _compact_history(
        self,
        original_messages: list[Message],
        prepared_messages: list[Message],
    ) -> list[Message]:

        if len(prepared_messages) <= 1:
            return prepared_messages

        transcript_path = (
            self.transcripts
            .write_snapshot(
                original_messages
            )
        )

        start = max(
            1,
            len(prepared_messages)- self.config.keep_recent_messages,
        )

        while (
            start > 0
            and prepared_messages[start].role
            == "tool"
        ):
            start -= 1

        old_messages = (
            prepared_messages[:start]
        )

        recent_messages = (
            prepared_messages[start:]
        )

        if not old_messages:
            return prepared_messages

        summary = self._summarize(
            old_messages
        )

        summary_message = Message(
            role="user",

            content=(
                "[Compacted conversation history]\n"
                f"{summary}\n\n"
                "[Full transcript archive]\n"
                f"{transcript_path}"
            ),
        )

        return [
            summary_message,
            *recent_messages,
        ]

    def _summarize(
        self,
        messages: list[Message],
    ) -> str:

        rendered = "\n".join(
            json.dumps(
                message_to_dict(
                    message
                ),
                ensure_ascii=False,
            )
            for message in messages
        )

        rendered = (
            self._limit_summary_input(
                rendered
            )
        )

        prompt = (
            "Summarize the following "
            "coding-agent history for "
            "future continuation.\n\n"

            "Preserve:\n"
            "- the user's actual goal\n"
            "- important decisions\n"
            "- files inspected or changed\n"
            "- tool results that affect decisions\n"
            "- errors and failed approaches\n"
            "- remaining work\n"
            "- important constraints\n\n"

            "Do not invent facts.\n\n"

            f"{rendered}"
        )

        response = self.model.complete(
            messages=[
                Message(
                    role="user",
                    content=prompt,
                )
            ],
            tools=None,
            system=(
                "You summarize coding-agent "
                "history accurately and "
                "concisely."
            ),
        )

        return (
            response.content.strip()
            or "(summary unavailable)"
        )

    def _limit_summary_input(
        self,
        text: str,
    ) -> str:

        limit = (
            self.config
            .summary_input_limit
        )

        if len(text) <= limit:
            return text

        head = limit // 4

        tail = (
            limit - head
        )

        return (
            text[:head]
            + "\n\n"
            + "...[middle omitted]..."
            + "\n\n"
            + text[-tail:]
        )