"""V6.3：统一 MCP 业务错误、参数错误、传输失败与超时等执行结果。"""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from devpilot.mcp.results import MCPContentMeta, MCPToolResult


class MCPExecutionStatus(str, Enum):
    """枚举一次 MCP 调用的不同终态，避免把所有异常都当成普通工具失败。"""

    SUCCESS = "success"
    TOOL_ERROR = "tool_error"
    VALIDATION_ERROR = "validation_error"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT_UNKNOWN = "timeout_unknown"
    TIMEOUT_NOT_STARTED = "timeout_not_started"
    CANCELLED_NOT_STARTED = "cancelled_not_started"
    CANCELLED_UNCONFIRMED = "cancelled_unconfirmed"


@dataclass(frozen=True)
class MCPExecutionOutcome:
    """表示一次工具调用的状态、数据和可观察的失败原因。"""

    name: str
    status: MCPExecutionStatus
    text_blocks: tuple[str, ...] = ()
    structured_content: dict[str, Any] | None = None
    other_content_types: tuple[str, ...] = ()
    content_metadata: tuple[MCPContentMeta, ...] = ()
    detail: str = ""

    @classmethod
    def from_result(cls, value: MCPToolResult) -> "MCPExecutionOutcome":
        """将 V6.1 的工具返回结果转换为成功或服务端业务错误。"""
        return cls(
            name=value.name,
            status=MCPExecutionStatus.TOOL_ERROR if value.is_error else MCPExecutionStatus.SUCCESS,
            text_blocks=value.text_blocks,
            structured_content=value.structured_content,
            other_content_types=value.other_content_types,
            content_metadata=getattr(value, "content_metadata", ()),
        )


class MCPResultFormatter:
    """将结构化执行结果压缩成有来源标记和字符上限的 V1 ToolResult 文本。"""

    def __init__(self, max_chars: int = 12000, max_blocks: int = 20):
        """初始化模型可见的字符预算和文本块数量限制。"""
        if max_chars < 64 or max_blocks < 1:
            raise ValueError("max_chars must be >=64 and max_blocks must be >=1")
        self.max_chars = max_chars
        self.max_blocks = max_blocks

    def format(self, outcome: MCPExecutionOutcome) -> str:
        """逐块生成受字符预算限制的模型文本，标明外部来源和准确的执行状态。"""
        status_prefixes = {
            MCPExecutionStatus.SUCCESS: "MCP RESULT",
            MCPExecutionStatus.TOOL_ERROR: "MCP TOOL ERROR",
            MCPExecutionStatus.VALIDATION_ERROR: "MCP_VALIDATION_ERROR",
            MCPExecutionStatus.TRANSPORT_ERROR: "MCP_TRANSPORT_ERROR",
            MCPExecutionStatus.TIMEOUT_UNKNOWN: "MCP_TIMEOUT_UNKNOWN",
            MCPExecutionStatus.TIMEOUT_NOT_STARTED: "MCP_TIMEOUT_NOT_STARTED",
            MCPExecutionStatus.CANCELLED_NOT_STARTED: "MCP_CANCELLED_NOT_STARTED",
            MCPExecutionStatus.CANCELLED_UNCONFIRMED: "MCP_CANCELLED_UNCONFIRMED",
        }
        prefix = f"[{status_prefixes[outcome.status]}: {outcome.name}; external untrusted data]\n"
        budget = self.max_chars - len(prefix)
        if budget <= 0:
            return prefix[: self.max_chars]

        def chunks():
            """依次输出文本、结构化 JSON 和非文本元数据，避免先拼接巨大的外部结果。"""
            first = True
            for block in outcome.text_blocks[: self.max_blocks]:
                if not first:
                    yield "\n"
                yield block
                first = False
            if len(outcome.text_blocks) > self.max_blocks:
                yield "\n" if not first else ""
                yield f"[omitted {len(outcome.text_blocks) - self.max_blocks} text blocks]"
                first = False
            if outcome.structured_content is not None:
                yield "\n" if not first else ""
                yield "structured_content: "
                yield from json.JSONEncoder(ensure_ascii=False, default=str).iterencode(outcome.structured_content)
                first = False
            if outcome.content_metadata:
                yield "\n" if not first else ""
                yield "non_text_metadata: "
                yield from json.JSONEncoder(ensure_ascii=False).iterencode([vars(x) for x in outcome.content_metadata])
                first = False
            elif outcome.other_content_types:
                yield "\n" if not first else ""
                yield "[unhandled content types: " + ", ".join(outcome.other_content_types) + "]"
                first = False
            if outcome.detail:
                yield "\n" if not first else ""
                yield "detail: " + outcome.detail

        pieces: list[str] = []
        total = 0
        truncated = False
        for chunk in chunks():
            if total + len(chunk) > budget:
                pieces.append(chunk[: max(0, budget - total)])
                truncated = True
                break
            pieces.append(chunk)
            total += len(chunk)
        body = "".join(pieces)
        if truncated:
            suffix = "\n[output truncated]"
            if budget >= len(suffix):
                body = body[: budget - len(suffix)] + suffix
        return prefix + body
