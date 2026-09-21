from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devpilot.tools.base import Tool


@dataclass
class SkillInfo:
    name: str
    description: str
    path: Path


class SkillCatalog:

    def __init__(self,root: Path):
        self.root = root.resolve()
        self._skills: dict[
            str,
            SkillInfo,
        ] = {}

        self.scan()

    def scan(self) -> None:
        # 清空旧缓存
        self._skills.clear()
        # 1. 直接定位顶层根目录下的 skills 文件夹
        skills_dir = self.root
        # 2. 如果 skills 文件夹不存在，直接安全退出
        if not skills_dir.is_dir():
            print("skills目录不对")
            return
        # 3. 遍历 skills 文件夹内部的各个子技能目录（如 debugging, testing）
        for directory in skills_dir.iterdir():
            if not directory.is_dir():
                continue
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            text = skill_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
            description = self._extract_description(text)
            info = SkillInfo(
                name=directory.name,
                description=description,
                path=skill_file,
            )
            self._skills[info.name] = info

    @staticmethod
    def _extract_description(
        text: str,
    ) -> str:

        for line in text.splitlines():

            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith("#"):
                continue

            if stripped == "---":
                continue

            return stripped[:200]

        return (
            "No description provided."
        )

    def get(
        self,
        name: str,
    ) -> SkillInfo | None:

        return self._skills.get(name)

    def load(
        self,
        name: str,
    ) -> str:

        skill = self.get(name)

        if skill is None:
            return (
                f"Error: unknown skill "
                f"'{name}'"
            )

        return skill.path.read_text(
            encoding="utf-8",
            errors="replace",
        )

    def catalog_text(self) -> str:

        if not self._skills:
            return "(no skills available)"

        lines = []

        for skill in (
            self._skills.values()
        ):

            lines.append(
                f"- {skill.name}: "
                f"{skill.description}"
            )

        return "\n".join(lines)


def create_load_skill_tool(
    catalog: SkillCatalog,
) -> Tool:

    def handler(
        arguments: dict[str, Any],
    ) -> str:

        name = arguments.get("name")

        if not isinstance(name, str):
            return (
                "Error: skill name "
                "must be string"
            )

        return catalog.load(name)

    return Tool(
        name="load_skill",

        description=(
            "Load the full instructions "
            "for one available skill. "
            "Use only when that skill is "
            "relevant to the current task."
        ),

        parameters={
            "type": "object",

            "properties": {
                "name": {
                    "type": "string"
                }
            },

            "required": [
                "name"
            ],

            "additionalProperties": False,
        },

        handler=handler,
    )