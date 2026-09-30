import json
import re
from dataclasses import dataclass

from devpilot.core.message import Message
from devpilot.core.model import Model


VALID_CATEGORIES = {
    "project_fact",
    "decision",
    "constraint",
    "preference",
    "lesson",
}


@dataclass
class ExtractedMemory:
    content: str
    category: str
    tags: list[str]


class MemoryExtractor:

    def __init__(self,model: Model):
        self.model = model

    def extract(
        self,
        user_input: str,
        assistant_output: str,
    ) -> list[ExtractedMemory]:

        prompt = f"""
Analyze this completed interaction.

User:
{user_input}

Assistant:
{assistant_output}

Extract only information that is likely
to remain useful in future sessions.

Good memory examples:
- stable project facts
- architecture decisions
- explicit constraints
- important lessons
- durable user/project preferences

Do NOT store:
- greetings
- temporary progress
- obvious transient tool output
- information useful only in this turn

Return ONLY a JSON array.

Each item must be:

{{
  "content": "...",
  "category": "project_fact|decision|constraint|preference|lesson",
  "tags": ["...", "..."]
}}

If nothing deserves long-term storage,
return [].
""".strip()

        response = self.model.complete(
            messages=[
                Message(
                    role="user",
                    content=prompt,
                )
            ],
            tools=None,
            system=(
                "You extract durable "
                "project memory. "
                "Return valid JSON only."
            ),
        )
        raw = response.content.strip()
        raw = self._strip_code_fence(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            if not isinstance(item,dict):
                continue
            content = item.get("content")
            category = item.get("category")
            tags = item.get("tags",[])
            if not isinstance(content,str):
                continue
            if (category not in VALID_CATEGORIES):
                continue
            if not isinstance(tags,list):
                tags = []
            tags = [
                tag
                for tag in tags
                if isinstance(tag,str)
            ]
            results.append(
                ExtractedMemory(
                    content=content,
                    category=category,
                    tags=tags,
                )
            )

        return results

    @staticmethod
    def _strip_code_fence(
        text: str,
    ) -> str:

        match = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.DOTALL,
        )

        if match:
            return match.group(1)

        return text