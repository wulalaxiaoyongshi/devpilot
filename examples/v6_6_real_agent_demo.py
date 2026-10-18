"""V6.6：无大模型费用的真实 V0/V1→MCP stdio 演示，可在原 DevPilot 工程中运行。"""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.mcp.config import LocalServerConfig
from devpilot.mcp.server_config import MCPServerDefinition
from devpilot.mcp.unified_app import UnifiedMCPApp
from devpilot.runtime.factory import create_app


class DemoModel:
    """在真正的 Agent.run 中返回 MCP ToolCall，收到工具结果后直接给用户展示。"""

    def __init__(self, agent_id: str):
        """为 Lead 与 Worker 保存独立模型身份。"""
        self.agent_id = agent_id

    def complete(self, messages, tools=None, system=None) -> ModelResponse:
        """Lead 首次选择外部加法工具，收到 ToolResult 后结束当前对话。"""
        last = messages[-1]
        if last.role == "tool":
            return ModelResponse(content=f"Agent received: {last.content}", tool_calls=[])
        if self.agent_id != "lead":
            return ModelResponse(content="Worker idle", tool_calls=[])
        return ModelResponse(content="I will call MCP", tool_calls=[ToolCall(id="demo-1", name="mcp__demo__add", arguments={"a": 6, "b": 7})])


def run_git(folder: Path, *args: str) -> None:
    """为示例创建隔离的 Git 仓库，不触碰实际项目历史。"""
    subprocess.run(["git", *args], cwd=folder, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    """构造 V5.7 App、挂载 V6 MCP Server，演示审批前拒绝和审批后真实调用。"""
    with TemporaryDirectory(prefix="devpilot-v66-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        run_git(repo, "init")
        run_git(repo, "config", "user.name", "DevPilot Demo")
        run_git(repo, "config", "user.email", "demo@example.com")
        (repo / "README.md").write_text("# DevPilot V6.6 Demo\n", encoding="utf-8")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "initial")
        approval = {"allowed": False}
        app = create_app(repo, DemoModel, approve=lambda name, args: approval["allowed"] and name == "mcp__demo__add", state_dir=root / "state")
        mcp_server = MCPServerDefinition("demo", local=LocalServerConfig.demo(), allow_tools=frozenset({"add"}))
        with UnifiedMCPApp(app, [mcp_server]) as runtime:
            print("[Approval denied]", runtime.chat("Use add"))
            approval["allowed"] = True
            print("[Approval granted]", runtime.chat("Use add"))
            print("[MCP Status]", runtime.statuses())
            print("[Background event pump]", runtime.pump(resume_lead=False))


if __name__ == "__main__":
    main()
