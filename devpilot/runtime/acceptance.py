"""V9.7：将不同业务场景的真实执行证据保存为可审查的最终验收报告。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SECRET = re.compile(r"(?i)(sk-[a-z0-9_-]{10,}|(?:api[_-]?key|authorization)\s*[=:]\s*[^\s,;]+)")


def safe_message(error: BaseException) -> str:
    """在报告异常时隐藏常见凭据样式，并限制单条错误的长度。"""
    return _SECRET.sub("[REDACTED]", f"{type(error).__name__}: {error}")[:400]


def case_passed(case: dict[str, Any]) -> bool:
    """只有当前场景所有预期检查都是真值且没有异常，才判为通过。"""
    checks = case.get("checks", {})
    return bool(checks) and all(value is True for value in checks.values()) and not case.get("error")


def build_final_report(mode: str, cases: list[dict[str, Any]], *, requires_mcp: bool = False) -> dict[str, Any]:
    """根据真实执行记录分别计算离线与在线结论，不允许从离线模式推导在线成功。"""
    if mode not in {"OFFLINE_SCRIPT_MODEL", "LIVE_DEEPSEEK"} or not cases:
        raise ValueError("Invalid acceptance mode or empty case list")
    names = [row.get("name") for row in cases]
    if len(names) != len(set(names)) or set(names) != {"analysis", "bugfix", "feature", "failure_guards"}:
        raise ValueError("Final acceptance requires four unique scenarios")
    passed = all(case_passed(row) for row in cases)
    if mode == "LIVE_DEEPSEEK":
        passed = passed and all(row.get("real_model_calls", 0) > 0 for row in cases if row["name"] != "failure_guards")
    if requires_mcp:
        passed = passed and any(row.get("mcp_verified") is True for row in cases)
    return {
        "schema": "devpilot-v9.7-acceptance-v1",
        "mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline_passed": bool(passed and mode == "OFFLINE_SCRIPT_MODEL"),
        "online_passed": bool(passed and mode == "LIVE_DEEPSEEK"),
        "requires_mcp": requires_mcp,
        "cases": cases,
        "scope": "isolated Python Git fixtures; no actual GitHub Issue or production deployment",
    }


def write_final_report(directory: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """原子保存脱敏 JSON 与人类可读 Markdown，并保持离线和在线结果独立。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "result.json"
    md_path = directory / "result.md"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    temp = json_path.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(json_path)
    lines = ["# DevPilot V9.7 最终集成验收", "", f"模式：{report['mode']}",
             f"离线通过：{report['offline_passed']}", f"真实模型在线通过：{report['online_passed']}",
             f"要求真实 MCP：{report['requires_mcp']}", ""]
    for row in report["cases"]:
        lines += [f"## {row['name']} — {'PASS' if case_passed(row) else 'FAIL'}", ""]
        for name, ok in row.get("checks", {}).items():
            lines.append(f"- {name}：{'PASS' if ok else 'FAIL'}")
        if row.get("error"):
            lines.append(f"- 错误：{row['error']}")
        lines.append("")
    temp_md = md_path.with_suffix(".tmp")
    temp_md.write_text("\n".join(lines), encoding="utf-8")
    temp_md.replace(md_path)
    return json_path, md_path
