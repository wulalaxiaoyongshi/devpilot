"""在不使用任何 API Key 的条件下验证轨迹记录和原 V1 权限接线。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def load_modules():
    """为无 openai SDK 的隔离单元环境准备导入替身，真实运行绝不使用。"""
    try:
        from openai import OpenAI
    except (ImportError, AttributeError):
        substitute = ModuleType("openai")
        substitute.OpenAI = object
        sys.modules["openai"] = substitute
    return (importlib.import_module("devpilot.runtime.model_trace"),
            importlib.import_module("examples.v9_7_real_github_mcp"))


def test_trace_writer_summary_counts_only_metadata(tmp_path):
    """轨迹摘要正确汇总模型请求、响应和 Token 且不包含输入正文。"""
    trace_mod, _ = load_modules()
    writer = trace_mod.TraceWriter(tmp_path / "trace.jsonl")
    writer.append("api_request", role="worker1", call_id="a")
    writer.append("api_response", role="worker1", call_id="a", tool_calls=["read_file"],
                  usage={"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20})
    report = writer.summarize()
    assert report["api_requests"] == report["api_responses"] == 1
    assert report["total_tokens"] == 20
    assert report["tool_calls_proposed"] == ["read_file"]


def test_model_trace_captures_actual_sdk_result_without_content(tmp_path):
    """伪造 SDK 响应只供单元测试使用，检查日志不记录真实请求的提示词和参数。"""
    trace_mod, _ = load_modules()
    from devpilot.core.message import Message
    class FakeCompletions:
        """隔离 SDK 返回结构的测试替身。"""
        def create(self, **request):
            """在记录请求字段后模拟一次带工具调用的返回。"""
            assert request["model"] == "test-model"
            assert request["messages"][0]["content"] == "private prompt"
            tool = SimpleNamespace(id="tc1", function=SimpleNamespace(name="read_file", arguments='{"path":"hello.py"}'))
            choice = SimpleNamespace(message=SimpleNamespace(content="private answer", tool_calls=[tool]), finish_reason="tool_calls")
            return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
                                   id="response-id-1", model="test-model")
    writer = trace_mod.TraceWriter(tmp_path / "trace.jsonl")
    model = object.__new__(trace_mod.TracedDeepSeekModel)
    model.role = "lead"
    model.model = "test-model"
    model.trace = writer
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    result = model.complete([Message(role="user", content="private prompt")], tools=[{"type": "function"}])
    assert result.tool_calls[0].arguments == {"path": "hello.py"}
    assert writer.summarize()["total_tokens"] == 5
    raw = writer.path.read_text(encoding="utf-8")
    assert "private prompt" not in raw and "private answer" not in raw and "hello.py" not in raw
    assert "response-id-1" in raw


def test_direct_mcp_uses_actual_permission_hook(monkeypatch, tmp_path):
    """复用真实 V1 Dispatcher 的 Hook，确认受信任 get 工具放行且非只读工具拒绝。"""
    _, probe = load_modules()
    from devpilot.mcp.multi_server import ServerState
    from devpilot.tools.base import Tool

    class FakeManager:
        """模拟连接态与发现清单但保留真实 Dispatcher、Policy 及 Adapter 边界。"""
        def __init__(self, registry, definitions):
            """向 Registry 注册一个只读和一个非只读的测试工具。"""
            self.tools = ["mcp__github__get_file_contents", "mcp__github__issue_read"]
            for name in self.tools:
                registry.register(Tool(name=name, description="test", parameters={"type": "object"},
                                       handler=lambda _args: "[MCP RESULT: get_file_contents]\npublic data"))
        def connect_all(self, strict=True):
            """模拟 MCP 连接建立。"""
            return None
        def statuses(self):
            """提供真实访问控制器所需的连接快照。"""
            return {"github": self.status("github")}
        def status(self, alias):
            """返回已连接的工具名称和代数。"""
            return SimpleNamespace(alias=alias, state=ServerState.CONNECTED, generation=1, tools=self.tools)
        def close(self):
            """标记隔离测试管理器的正常关闭。"""
            return None

    monkeypatch.setattr(probe, "MCPMultiServerManager", FakeManager)
    monkeypatch.setattr(probe, "load_server_definitions", lambda _config: [SimpleNamespace(alias="github")])
    allowed = probe.run_direct(Path("unused.json"), "github", "mcp__github__get_file_contents", {"path": "README.md"}, True)
    denied = probe.run_direct(Path("unused.json"), "github", "mcp__github__issue_read", {}, True)
    assert allowed["passed"] is True and allowed["v1_dispatch_status"] == "returned"
    assert denied["passed"] is False and denied["v1_dispatch_status"] == "denied"


def test_external_config_is_dynamic_and_never_contains_token():
    """GitHub 配置动态发现工具且只从环境变量注入认证，不包含硬编码密钥。"""
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "examples/github_mcp_readonly.json").read_text(encoding="utf-8"))
    row = data["servers"][0]
    assert row["allow_tools"] is None
    assert row["env_from"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert "GITHUB_READ_ONLY=1" in row["args"]
