"""V6.6：在用户真实 V0～V5.7 工程及已安装的 MCP SDK 中进行最终端到端验收。"""

import subprocess
from pathlib import Path

import pytest


def git(cwd: Path, *args: str) -> str:
    """在 pytest 临时目录执行 Git，避免修改用户项目或全局 Git 配置。"""
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def test_real_lead_agent_v1_permission_mcp_stdio_and_result(tmp_path: Path):
    """FakeModel 在真实 V0 Agent Loop 中生成 ToolCall，经 V1 权限和真实 stdio MCP Server 返回工具结果。"""
    pytest.importorskip("mcp", reason="需要官方 MCP Python SDK v2 才能运行真实 stdio 验收")
    pytest.importorskip("devpilot.runtime.factory", reason="需要用户安装自己的 V5.7 Unified Agent Runtime")
    from devpilot.core.message import ToolCall
    from devpilot.core.model import ModelResponse
    from devpilot.mcp.config import LocalServerConfig
    from devpilot.mcp.server_config import MCPServerDefinition
    from devpilot.mcp.unified_app import UnifiedMCPApp
    from devpilot.runtime.factory import create_app

    project = tmp_path / "project"
    project.mkdir()
    git(project, "init")
    git(project, "config", "user.name", "DevPilot V6.6 Test")
    git(project, "config", "user.email", "devpilot-v66@example.com")
    (project / "README.md").write_text("# MCP integration test\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-m", "initial")

    observed_tool_results: list[str] = []

    class ScriptedModel:
        """只替代付费模型的确定性决策，仍走用户工程真正的 Agent Loop 与 Dispatcher。"""

        def __init__(self, agent_id: str):
            """记住本 Agent 身份，以便只在 Lead 上生成 MCP ToolCall。"""
            self.agent_id = agent_id

        def complete(self, messages, tools=None, system=None) -> ModelResponse:
            """首次返回 MCP ToolCall，收到 V1 ToolResult 后保存结果并结束推理。"""
            last = messages[-1]
            if last.role == "tool":
                observed_tool_results.append(last.content)
                return ModelResponse(content=last.content, tool_calls=[])
            if self.agent_id != "lead":
                return ModelResponse(content="Worker idle", tool_calls=[])
            return ModelResponse(content="Calling MCP tool", tool_calls=[ToolCall(id="mcp-call-1", name="mcp__demo__add", arguments={"a": 9, "b": 12})])

    approval = {"allowed": False}
    app = create_app(project, ScriptedModel, approve=lambda name, arguments: approval["allowed"] and name == "mcp__demo__add", state_dir=tmp_path / "state")
    definition = MCPServerDefinition("demo", local=LocalServerConfig.demo(), allow_tools=frozenset({"add"}), timeout_seconds=15)
    with UnifiedMCPApp(app, [definition]) as runtime:
        rejected = runtime.chat("Try the MCP add tool without permission")
        assert "approval" in rejected.lower() or "denied" in rejected.lower()
        assert not any("MCP RESULT" in value for value in observed_tool_results)
        approval["allowed"] = True
        success = runtime.chat("Now use the MCP add tool")
        assert "MCP RESULT" in success
        assert "21" in success
        assert any("MCP RESULT" in value for value in observed_tool_results)
        assert runtime.statuses()["demo"].generation == 1
