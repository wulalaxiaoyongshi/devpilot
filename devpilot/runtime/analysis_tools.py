"""V9.2：只向代码分析 Lead 暴露经过路径约束的读取与任务规划工具。"""

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from devpilot.context.skills import SkillCatalog, SkillInfo
from devpilot.tools.base import Tool
from devpilot.tools.registry import ToolRegistry


ALLOWED_SUFFIXES = frozenset({".py", ".pyi", ".md", ".rst", ".toml", ".txt", ".yaml", ".yml", ".json", ".ini", ".cfg"})
PRIVATE_PARTS = frozenset({".git", ".devpilot", ".venv", "venv", ".idea", "__pycache__", ".ssh", "node_modules", "secrets", "credentials"})
PRIVATE_FILES = frozenset({".env", ".pypirc", ".npmrc", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"})
MAX_FILE_BYTES = 120_000
MAX_READ_CHARS = 50_000


def _safe_file(root: Path, name: str) -> Path | None:
    """限制到仓库内的普通源码文件，拒绝敏感目录、符号链接和越界路径。"""
    if not name or "\x00" in name or Path(name).is_absolute():
        return None
    raw = Path(name)
    if any(part.lower() in PRIVATE_PARTS or part in (".", "..") for part in raw.parts):
        return None
    if any(part.lower().startswith(".env") for part in raw.parts):
        return None
    if raw.name.lower() in PRIVATE_FILES or raw.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    target = root / raw
    if any(segment.is_symlink() for segment in (target, *target.parents) if segment != root and root in segment.parents):
        return None
    try:
        canonical = target.resolve()
        canonical.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    return canonical if canonical.is_file() else None



class SafeAnalysisSkillCatalog(SkillCatalog):
    """扫描 V2 Skill 时过滤目录链接与越界文件，避免在系统 Prompt 中泄露外部内容。"""

    def __init__(self, workspace: Path):
        """使用仓库根目录作为 Skill 访问边界，不跟随外部符号链接。"""
        self.workspace = workspace.resolve()
        self.root = self.workspace / "skills"
        self._skills: dict[str, SkillInfo] = {}
        self.scan()

    def scan(self) -> None:
        """只加载当前仓库内非链接 Skill 目录的有效 SKILL.md 元数据。"""
        self._skills.clear()
        if not self.root.is_dir() or self.root.is_symlink():
            return
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", directory.name):
                continue
            path = _safe_file(self.workspace, f"skills/{directory.name}/SKILL.md")
            if path is None or path.stat().st_size > MAX_FILE_BYTES:
                continue
            description = self._extract_description(path.read_text(encoding="utf-8", errors="replace"))
            self._skills[directory.name] = SkillInfo(directory.name, description, path)

def create_analysis_registry(root: Path, source: ToolRegistry) -> ToolRegistry:
    """创建独立的最小权限工具表，绝不把写文件或 Shell 注册给分析 Lead。"""
    workspace = root.resolve()
    selected = ToolRegistry()

    def read_code(arguments: dict[str, Any]) -> str:
        """读取仓库内部允许的源码文本，并限制文件和输出大小。"""
        name = arguments.get("path")
        if not isinstance(name, str):
            return "Error: path must be string"
        target = _safe_file(workspace, name)
        if target is None:
            return "Error: file is unavailable or excluded from analysis"
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                return "Error: source file exceeds analysis size limit"
            data = target.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return f"Error: cannot read file: {type(error).__name__}"
        return data[:MAX_READ_CHARS] + ("\n[File truncated]" if len(data) > MAX_READ_CHARS else "")

    def list_code(arguments: dict[str, Any]) -> str:
        """只列出安全的项目文件路径，不显示 Git 元数据和私密文件。"""
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern or Path(pattern).is_absolute():
            return "Error: invalid glob pattern"
        if any(part == ".." or part.lower() in PRIVATE_PARTS or part.lower().startswith(".env") for part in Path(pattern).parts):
            return "Error: pattern accesses excluded directory"
        matches = []
        for directory, subdirs, filenames in os.walk(workspace, followlinks=False):
            subdirs[:] = [name for name in subdirs if name.lower() not in PRIVATE_PARTS
                          and not name.lower().startswith(".env") and not (Path(directory) / name).is_symlink()]
            for filename in filenames:
                candidate = Path(directory) / filename
                relative = candidate.relative_to(workspace).as_posix()
                if (fnmatch.fnmatchcase(relative, pattern)
                        or (pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]))):
                    if _safe_file(workspace, relative) is not None:
                        matches.append(relative)
                if len(matches) >= 200:
                    break
            if len(matches) >= 200:
                break
        return "\n".join(sorted(set(matches))) if matches else "(no matches)"

    def load_safe_skill(arguments: dict[str, Any]) -> str:
        """限制 Skill 名称和实际文件位置，拒绝经符号链接读取仓库外内容。"""
        name = arguments.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return "Error: invalid skill name"
        if _safe_file(workspace, f"skills/{name}/SKILL.md") is None:
            return "Error: skill is unavailable or excluded"
        source_tool = source.get("load_skill")
        if source_tool is None:
            return "Error: load_skill is not registered"
        return source_tool.handler(arguments)

    selected.register(Tool("read_file", "Read a non-secret source file in this repository; never read credentials.", {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False,
    }, read_code))
    selected.register(Tool("glob", "List non-secret source files in this repository.", {
        "type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"], "additionalProperties": False,
    }, list_code))
    skill = source.get("load_skill")
    if skill is not None:
        selected.register(Tool(skill.name, skill.description, skill.parameters, load_safe_skill))
    for name in ("todo_write", "memory_search", "task_create", "task_list"):
        existing = source.get(name)
        if existing is None:
            raise RuntimeError(f"Analysis mode requires tool: {name}")
        selected.register(existing)
    return selected
