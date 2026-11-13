"""为真实 DeepSeek 请求保存最小化的可核对调用轨迹，不记录提示词和密钥。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from devpilot.core.message import Message, ToolCall
from devpilot.core.model import DeepSeekModel, ModelResponse


class TraceWriter:
    """将模型与工具的执行元信息写到仓库外的 JSONL 文件。"""

    def __init__(self, path: Path):
        """建立轨迹文件和进程内写锁，避免多 Worker 写入交错。"""
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.session_id = uuid4().hex

    def append(self, kind: str, **fields: Any) -> None:
        """只持久化调用标识、Token 用量和工具名称等非内容字段。"""
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "kind": kind,
            **fields,
        }
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def summarize(self) -> dict[str, Any]:
        """统计当前轨迹文件中的真实响应、模型角色、工具调用和 API Token 用量。"""
        records = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        records.append(item)
                except json.JSONDecodeError:
                    continue
        responses = [x for x in records if x.get("kind") == "api_response"]
        requests = [x for x in records if x.get("kind") == "api_request"]
        calls = [tool for x in responses for tool in x.get("tool_calls", [])]
        executions = [x for x in records if x.get("kind") == "mcp_tool_executed"]
        usages = [x.get("usage") or {} for x in responses]
        return {
            "trace_file": str(self.path),
            "api_requests": len(requests),
            "api_responses": len(responses),
            "api_errors": sum(x.get("kind") == "api_error" for x in records),
            "model_roles": sorted({str(x.get("role")) for x in responses}),
            "tool_calls_proposed": calls,
            "mcp_tools_executed": [x.get("name") for x in executions if x.get("success")],
            "usage_reported": any(any(isinstance(v, int) for v in u.values()) for u in usages),
            "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usages),
            "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usages),
            "total_tokens": sum(u.get("total_tokens") or 0 for u in usages),
        }


class TracedDeepSeekModel(DeepSeekModel):
    """使用原 DeepSeek 连接配置实际请求 API，再记录可核对的响应 ID 和工具计划。"""

    def __init__(self, model: str, *, role: str, trace: TraceWriter):
        """延用原 DeepSeekModel 的 API 连接与认证机制并绑定执行角色。"""
        super().__init__(model=model)
        self.role = role
        self.trace = trace

    def complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                 system: str | None = None) -> ModelResponse:
        """真实请求 DeepSeek，并记录 API 响应、Token 和模型提出的工具名而不保存内容。"""
        api_messages = [self._to_api_message(message) for message in messages]
        if system:
            api_messages.insert(0, {"role": "system", "content": system})
        request: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        call_id = uuid4().hex
        self.trace.append("api_request", call_id=call_id, role=self.role,
                          model=self.model, messages_count=len(api_messages),
                          tools_offered=len(tools or []))
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as error:
            self.trace.append("api_error", call_id=call_id, role=self.role,
                              error_type=type(error).__name__, elapsed_ms=round((time.perf_counter()-started)*1000))
            raise
        message = response.choices[0].message
        parsed_calls: list[ToolCall] = []
        for item in message.tool_calls or []:
            try:
                arguments = json.loads(item.function.arguments)
            except (ValueError, TypeError) as error:
                self.trace.append("parse_error", call_id=call_id, role=self.role,
                                  tool_name=item.function.name, error_type=type(error).__name__)
                raise ValueError("Model returned invalid tool arguments") from error
            parsed_calls.append(ToolCall(id=item.id, name=item.function.name, arguments=arguments))
        usage_obj = getattr(response, "usage", None)
        usage = {key: getattr(usage_obj, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        self.trace.append(
            "api_response", call_id=call_id, role=self.role, request_model=self.model,
            response_model=str(getattr(response, "model", "")),
            response_id=str(getattr(response, "id", "")),
            elapsed_ms=round((time.perf_counter()-started)*1000),
            finish_reason=str(getattr(response.choices[0], "finish_reason", "")),
            usage=usage, tool_calls=[call.name for call in parsed_calls],
            tool_call_ids=[call.id for call in parsed_calls],
            tool_results_in_context=sum(msg.role == "tool" for msg in messages),
            answer_chars=len(message.content or ""),
        )
        return ModelResponse(content=message.content or "", tool_calls=parsed_calls)
