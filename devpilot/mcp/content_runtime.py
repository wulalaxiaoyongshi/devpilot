"""V6.5：宿主可控地发现、读取并过滤 MCP Resources 与 Prompts。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceInfo:
    """表示 Server 声明的资源名称、URI 和 MIME 信息。"""

    name: str
    uri: str
    mime_type: str | None
    description: str


@dataclass(frozen=True)
class TemplateInfo:
    """表示尚未填入参数的 URI 模板。"""

    name: str
    uri_template: str
    mime_type: str | None


@dataclass(frozen=True)
class PromptInfo:
    """表示一个可供宿主或用户选择的 Prompt 模板。"""

    name: str
    description: str
    required_arguments: tuple[str, ...]


@dataclass(frozen=True)
class ResourceContent:
    """保存已读取的文字与非文字元信息，但不自动注入模型。"""

    uri: str
    text: str
    mime_types: tuple[str | None, ...]
    binary_types: tuple[str, ...]
    truncated: bool
    source: str = "external_untrusted_mcp_resource"


@dataclass(frozen=True)
class PromptContent:
    """保存 MCP Server 渲染后的消息，仅供宿主审阅后选择性使用。"""

    name: str
    messages: tuple[tuple[str, str], ...]
    other_types: tuple[str, ...]
    truncated: bool
    source: str = "external_untrusted_mcp_prompt"


class MCPContentRuntime:
    """在已连接的 MCPBridge 上读取允许的资源与提示模板，限制数量和内容大小。"""

    def __init__(self, bridge: Any, allowed_uri_prefixes: tuple[str, ...] = (), allowed_prompts: frozenset[str] = frozenset(), max_pages: int = 8, max_items: int = 100, max_chars: int = 12000):
        """从可信宿主接收资源 URI 白名单、Prompt 白名单和上下文限额。"""
        if not 1 <= max_pages <= 100 or not 1 <= max_items <= 1000 or not 1 <= max_chars <= 100000:
            raise ValueError("Invalid MCP content limits")
        if any(not isinstance(prefix, str) or "://" not in prefix for prefix in allowed_uri_prefixes):
            raise ValueError("Every URI prefix must contain a scheme")
        self.bridge = bridge
        self.allowed_uri_prefixes = tuple(allowed_uri_prefixes)
        self.allowed_prompts = frozenset(allowed_prompts)
        self.max_pages = max_pages
        self.max_items = max_items
        self.max_chars = max_chars

    def _collect_pages(self, method_name: str, field: str) -> list[Any]:
        """沿着 MCP 分页游标读取目录，并拒绝无限分页和重复游标。"""
        all_items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(self.max_pages):
            page = getattr(self.bridge, method_name)(cursor=cursor)
            new_items = list(getattr(page, field))
            if len(all_items) + len(new_items) > self.max_items:
                raise ValueError("MCP catalog exceeded configured max_items")
            all_items.extend(new_items)
            cursor = getattr(page, "next_cursor", None)
            if cursor is None:
                return all_items
            if cursor in seen:
                raise ValueError("MCP catalog contains repeating cursor")
            seen.add(cursor)
        raise ValueError("MCP catalog exceeded configured max_pages")

    def list_resources(self) -> list[ResourceInfo]:
        """发现所有分页资源，仅返回 URI 获宿主允许的目录项。"""
        return [ResourceInfo(str(item.name), str(item.uri), getattr(item, "mime_type", None), getattr(item, "description", "") or "") for item in self._collect_pages("list_resources", "resources") if self._allowed_uri(str(item.uri))]

    def list_resource_templates(self) -> list[TemplateInfo]:
        """读取 Server 声明的 URI 模板，不自动展开可能无限的资源集合。"""
        return [TemplateInfo(str(item.name), str(item.uri_template), getattr(item, "mime_type", None)) for item in self._collect_pages("list_resource_templates", "resource_templates") if self._allowed_uri(str(item.uri_template))]

    def list_prompts(self) -> list[PromptInfo]:
        """列出宿主白名单中的 Prompt 模板及其必填参数。"""
        return [PromptInfo(str(item.name), getattr(item, "description", "") or "", tuple(str(arg.name) for arg in (getattr(item, "arguments", None) or ()) if getattr(arg, "required", False))) for item in self._collect_pages("list_prompts", "prompts") if str(item.name) in self.allowed_prompts]

    def _allowed_uri(self, uri: str) -> bool:
        """仅允许访问可信宿主明确配置的 URI 前缀。"""
        return any(uri.startswith(prefix) for prefix in self.allowed_uri_prefixes)

    def _limit(self, text: str) -> tuple[str, bool]:
        """以最大字符数截断来自 MCP Server 的大文本。"""
        return text[:self.max_chars], len(text) > self.max_chars

    def read_resource(self, uri: str) -> ResourceContent:
        """从允许的 URI 读取资源，保留 Blob 类型但不将其解码并塞入模型上下文。"""
        if not self._allowed_uri(uri):
            raise PermissionError("MCP resource URI not approved by host")
        response = self.bridge.read_resource(uri)
        blocks = list(response.contents)
        texts = [block.text for block in blocks if getattr(block, "text", None) is not None]
        text, clipped = self._limit("\n".join(texts))
        mime_types = tuple(getattr(block, "mime_type", None) for block in blocks)
        nontext = tuple(str(getattr(block, "mime_type", "unknown")) for block in blocks if getattr(block, "text", None) is None)
        return ResourceContent(uri, text, mime_types, nontext, clipped)

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> PromptContent:
        """只有宿主选中的模板才能渲染，并保留外部消息来源与长度上限。"""
        if name not in self.allowed_prompts:
            raise PermissionError("MCP prompt not approved by host")
        args = arguments or {}
        if not isinstance(args, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in args.items()):
            raise ValueError("MCP prompt arguments must be string-to-string")
        response = self.bridge.get_prompt(name, args)
        messages: list[tuple[str, str]] = []
        others: list[str] = []
        remain = self.max_chars
        clipped = False
        for message in response.messages:
            if len(messages) + len(others) >= self.max_items:
                raise ValueError("MCP prompt exceeded configured max_items")
            role = str(getattr(message, "role", "user"))
            content = message.content
            if getattr(content, "type", "") != "text":
                others.append(str(getattr(content, "type", "unknown")))
                continue
            full = content.text
            segment = full[:remain]
            clipped = clipped or len(full) > remain
            remain -= len(segment)
            messages.append((role, segment))
        return PromptContent(name, tuple(messages), tuple(others), clipped)
