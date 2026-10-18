"""V6.4：声明多 Server 的可信配置，并从 JSON 安全装载本地 stdio 服务。"""

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from devpilot.mcp.adapter import v1_tool_name
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.content_client import RemoteServerConfig

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BASE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")


@dataclass(frozen=True)
class MCPServerDefinition:
    """定义一台 MCP Server 的唯一别名、启动命令、工具白名单与执行限额。"""

    alias: str
    local: LocalServerConfig | None = field(default=None, repr=False)
    allow_tools: frozenset[str] | None = None
    timeout_seconds: float = 30.0
    max_tools: int = 100
    remote: RemoteServerConfig | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """创建配置时检查别名、允许的工具名和限额，避免将坏配置交给连接管理器。"""
        if (self.local is None) == (self.remote is None):
            raise ValueError("Specify exactly one local or remote MCP Server transport")
        v1_tool_name(self.alias, "placeholder")
        if self.allow_tools is not None:
            object.__setattr__(self, "allow_tools", frozenset(self.allow_tools))
            for tool in self.allow_tools:
                v1_tool_name(self.alias, tool)
        if not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        if type(self.max_tools) is not int or self.max_tools < 1 or self.max_tools > 500:
            raise ValueError("max_tools must be between 1 and 500")


def load_server_definitions(path: Path, environment: Mapping[str, str] | None = None, max_servers: int = 16) -> list[MCPServerDefinition]:
    """宿主启动时从受信任 JSON 文件读取多台 Server，并只透传明确许可的环境变量。"""
    source = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(source, dict) or set(source) != {"servers"} or not isinstance(source["servers"], list):
        raise ValueError("MCP configuration must contain only a servers array")
    rows = source["servers"]
    if type(max_servers) is not int or max_servers < 1 or len(rows) > max_servers:
        raise ValueError("Too many MCP servers or invalid max_servers")
    host_env = os.environ if environment is None else environment
    definitions: list[MCPServerDefinition] = []
    aliases: set[str] = set()
    permitted_fields = {"alias", "command", "args", "env_from", "allow_tools", "timeout_seconds", "max_tools", "url"}
    for row in rows:
        if not isinstance(row, dict) or set(row) - permitted_fields:
            raise ValueError("Unknown MCP Server configuration field")
        alias = row.get("alias")
        command = row.get("command")
        args = row.get("args", [])
        env_names = row.get("env_from", [])
        allow = row.get("allow_tools")
        remote_url = row.get("url")
        if not isinstance(alias, str) or ((command is None) == (remote_url is None)):
            raise ValueError("Every MCP Server needs an alias and exactly one command or url")
        if command is not None and not isinstance(command, str):
            raise ValueError("Server command must be a string")
        if remote_url is not None and (not isinstance(remote_url, str) or "args" in row or "env_from" in row):
            raise ValueError("Remote Server requires a URL and cannot use subprocess args or env_from")
        if alias in aliases:
            raise ValueError(f"Duplicate MCP Server alias: {alias}")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError(f"Invalid args for MCP Server {alias}")
        if not isinstance(env_names, list) or any(not isinstance(key, str) or not _ENV_NAME.fullmatch(key) for key in env_names):
            raise ValueError(f"Invalid env_from for MCP Server {alias}")
        if not isinstance(allow, (list, type(None))) or (allow is not None and any(not isinstance(tool, str) for tool in allow)):
            raise ValueError(f"Invalid allow_tools for MCP Server {alias}")
        names = set(env_names)
        if len(names) != len(env_names):
            raise ValueError(f"Duplicate environment name for MCP Server {alias}")
        missing = [key for key in names if key not in host_env]
        if missing:
            raise ValueError(f"Missing environment variables for {alias}: {', '.join(sorted(missing))}")
        child_env = {key: host_env[key] for key in _BASE_ENV if key in host_env}
        child_env.update({key: host_env[key] for key in names})
        local = LocalServerConfig(command=command, args=tuple(args), env=child_env) if command is not None else None
        remote = RemoteServerConfig(remote_url) if remote_url is not None else None
        definition = MCPServerDefinition(
            alias=alias,
            local=local,
            remote=remote,
            allow_tools=None if allow is None else frozenset(allow),
            timeout_seconds=row.get("timeout_seconds", 30.0),
            max_tools=row.get("max_tools", 100),
        )
        definitions.append(definition)
        aliases.add(alias)
    return definitions
