"""V9.5：对已发现 MCP 工具按可信 Server 和风险等级动态实施最小授权。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from devpilot.mcp.adapter import v1_tool_name
from devpilot.mcp.multi_server import ServerState

_READ = re.compile(r"^(get|list|read|search|fetch|query)_[A-Za-z0-9_-]+$", re.IGNORECASE)
_UNSAFE = re.compile(r"(?:^|[_-])(delete|remove|drop|exec|execute|run|write|update|create|push|merge|reset|secret|token|admin|grant|revoke)(?:$|[_-])", re.IGNORECASE)
_SENSITIVE_ARGS = frozenset({"command", "shell", "script", "code", "mutation", "sql", "execute"})


@dataclass(frozen=True)
class MCPServerAccess:
    """由可信宿主授权某台 Server 的只读访问与人工审批权限。"""

    alias: str
    auto_read: bool = False
    allow_reviewed_mutations: bool = False
    max_argument_bytes: int = 8192

    def __post_init__(self) -> None:
        """在使用授权配置前拒绝非法 Server 别名和参数大小上限。"""
        v1_tool_name(self.alias, "test")
        if type(self.auto_read) is not bool or type(self.allow_reviewed_mutations) is not bool:
            raise TypeError("MCP access flags must be booleans")
        if type(self.max_argument_bytes) is not int or not 1 <= self.max_argument_bytes <= 65536:
            raise ValueError("max_argument_bytes must be between 1 and 65536")


class MCPAccessController:
    """只对当前已连接且经宿主刷新发现清单的 MCP 工具进行动态授权。"""

    def __init__(self, rules: tuple[MCPServerAccess, ...], *, approve_mutation: Callable[[str, dict], bool] | None = None):
        """保存可信宿主的 Server 级策略，默认拒绝所有未知工具。"""
        if len(set(rule.alias for rule in rules)) != len(rules):
            raise ValueError("Duplicate MCP access Server alias")
        self.rules = {rule.alias: rule for rule in rules}
        self.approve_mutation = approve_mutation
        self.manager: Any | None = None
        self.discovered: dict[str, tuple[int, frozenset[str]]] = {}

    def sync_discovery(self, manager: Any) -> None:
        """从真实多 Server 管理器取得已连接工具快照并记录连接代数。"""
        self.manager = manager
        self.discovered = {
            alias: (status.generation, frozenset(status.tools))
            for alias, status in manager.statuses().items()
            if alias in self.rules and status.state == ServerState.CONNECTED
        }

    def authorize(self, name: str, arguments: dict[str, Any]) -> bool:
        """已发现的可信只读工具自动放行，写操作须经本次可信宿主审批。"""
        if self.manager is None or not isinstance(name, str) or not isinstance(arguments, dict):
            return False
        for alias, rule in self.rules.items():
            prefix = f"mcp__{alias}__"
            if not name.startswith(prefix):
                continue
            snapshot = self.discovered.get(alias)
            if snapshot is None or name not in snapshot[1]:
                return False
            try:
                status = self.manager.status(alias)
            except (KeyError, RuntimeError, ValueError):
                return False
            if status.state != ServerState.CONNECTED or status.generation != snapshot[0] or name not in status.tools:
                return False
            try:
                encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, OverflowError):
                return False
            if len(encoded.encode("utf-8")) > rule.max_argument_bytes:
                return False
            remote = name[len(prefix):]
            safe_read_name = _READ.fullmatch(remote) and not _UNSAFE.search(remote)
            safe_read_args = not any(key.lower() in _SENSITIVE_ARGS for key in arguments if isinstance(key, str))
            if rule.auto_read and safe_read_name and safe_read_args:
                return True
            if rule.allow_reviewed_mutations and self.approve_mutation is not None:
                return self.approve_mutation(name, arguments) is True
            return False
        return False
