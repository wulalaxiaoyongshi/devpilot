"""V6.3：在保留 V6.1 返回结构的同时，增加非文本内容的安全元信息。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MCPToolInfo:
    """保存服务端发现的工具名称、说明和输入 Schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPContentMeta:
    """只记录图片、音频、资源等非文本块的元数据，不把原始二进制交给模型。"""

    type: str
    mime_type: str | None = None
    encoded_bytes: int | None = None
    uri: str | None = None


@dataclass(frozen=True)
class MCPToolResult:
    """保存一次 MCP 工具调用的文本、结构化数据、错误状态和可选的非文本元数据。"""

    name: str
    is_error: bool
    text_blocks: tuple[str, ...]
    structured_content: dict[str, Any] | None
    other_content_types: tuple[str, ...]
    content_metadata: tuple[MCPContentMeta, ...] = ()

    @property
    def text(self) -> str:
        """将工具返回的所有文本块按顺序拼成可阅读的字符串。"""
        return "\n".join(self.text_blocks)


def normalize_tool_result(name: str, raw: Any) -> MCPToolResult:
    """把 SDK 的 CallToolResult 转换为 DevPilot 结果，同时保留非文本块的类型和大小信息。"""
    text_blocks: list[str] = []
    other_types: list[str] = []
    metadata: list[MCPContentMeta] = []
    for block in raw.content:
        kind = str(getattr(block, "type", "unknown"))
        if kind == "text":
            text_blocks.append(str(block.text))
            continue
        other_types.append(kind)
        encoded = getattr(block, "data", None)
        metadata.append(
            MCPContentMeta(
                type=kind,
                mime_type=getattr(block, "mimeType", getattr(block, "mime_type", None)),
                encoded_bytes=len(encoded) if isinstance(encoded, (str, bytes)) else None,
                uri=str(getattr(block, "uri")) if getattr(block, "uri", None) is not None else None,
            )
        )
    structured = getattr(raw, "structured_content", getattr(raw, "structuredContent", None))
    if structured is not None and not isinstance(structured, dict):
        structured = {"value": structured}
    return MCPToolResult(
        name=name,
        is_error=bool(getattr(raw, "is_error", getattr(raw, "isError", False))),
        text_blocks=tuple(text_blocks),
        structured_content=structured,
        other_content_types=tuple(other_types),
        content_metadata=tuple(metadata),
    )
