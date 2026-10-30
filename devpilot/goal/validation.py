"""在写入 GoalStore 前，检查验收条件及其配置是否完整、安全、可序列化。"""

import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .model import GoalSpec

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_ALLOWED_KINDS = {"command_exit_code", "file_exists", "file_contains", "git_head", "manual"}
_ALLOWED_FIELDS = {
    "command_exit_code": {"argv", "expected_exit_code"},
    "file_exists": {"path"},
    "file_contains": {"path", "contains"},
    "git_head": {"expected_sha"},
    "manual": {"instructions"},
}


def json_snapshot(value: Any) -> Any:
    """把对象复制成严格的 JSON 数据，拒绝不能持久化的值和 NaN。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Value is not safe JSON: {exc}") from exc


def _require_relative_file(value: Any, label: str) -> None:
    """检查验收文件路径为工作区内的相对路径，避免路径穿越。"""
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty POSIX-style relative path")
    posix_path, windows_path = PurePosixPath(value), PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.drive or windows_path.root or ".." in posix_path.parts:
        raise ValueError(f"{label} cannot escape the workspace")
    if value == "." or any(part == "" for part in value.split("/")):
        raise ValueError(f"{label} must name a file inside the workspace")


def _validate_config(kind: str, config: dict[str, Any]) -> None:
    """按照验收类型校验其参数，只保存规则而不执行命令。"""
    if set(config) - _ALLOWED_FIELDS[kind]:
        raise ValueError(f"Unexpected config fields for {kind}: {sorted(set(config) - _ALLOWED_FIELDS[kind])}")
    if kind == "command_exit_code":
        argv = config.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("command_exit_code requires non-empty argv: list[str]")
        exit_code = config.get("expected_exit_code", 0)
        if type(exit_code) is not int:
            raise ValueError("expected_exit_code must be an integer")
    elif kind in {"file_exists", "file_contains"}:
        _require_relative_file(config.get("path"), "path")
        if kind == "file_contains" and (not isinstance(config.get("contains"), str) or not config["contains"]):
            raise ValueError("file_contains requires a non-empty contains string")
    elif kind == "git_head":
        if not isinstance(config.get("expected_sha"), str) or not _SHA.fullmatch(config["expected_sha"]):
            raise ValueError("git_head requires expected_sha of 7 to 40 hexadecimal characters")
    elif kind == "manual":
        if not isinstance(config.get("instructions"), str) or not config["instructions"].strip():
            raise ValueError("manual requires non-empty instructions")


def validate_spec(spec: GoalSpec) -> None:
    """检查目标 ID、验收条件、重复名称和每种条件的具体参数是否合法。"""
    if not isinstance(spec, GoalSpec):
        raise TypeError("Expected GoalSpec")
    if not _ID.fullmatch(spec.id) or not spec.title.strip() or not spec.description.strip():
        raise ValueError("Goal needs a valid id, title and description")
    if not isinstance(spec.criteria, tuple) or not spec.criteria:
        raise ValueError("Goal must have at least one criterion")
    if len(spec.criteria) > 128:
        raise ValueError("Goal cannot contain more than 128 criteria")
    names = set()
    required = False
    for item in spec.criteria:
        if not _ID.fullmatch(item.id) or not item.description.strip():
            raise ValueError("Criterion needs valid id and description")
        if item.id in names:
            raise ValueError(f"Duplicate criterion id: {item.id}")
        names.add(item.id)
        if item.kind not in _ALLOWED_KINDS:
            raise ValueError(f"Unsupported criterion kind: {item.kind}")
        if type(item.required) is not bool:
            raise TypeError("Criterion.required must be bool")
        if not isinstance(item.config, dict):
            raise TypeError("Criterion.config must be an object")
        _validate_config(item.kind, json_snapshot(item.config))
        required = required or item.required
    if not required:
        raise ValueError("Goal needs at least one required criterion")
