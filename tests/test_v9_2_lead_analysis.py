"""V9.2：测试 V0～V3 的真实装配、分析模式权限和结构化分发事实。"""

from pathlib import Path
import subprocess

import pytest

from devpilot.core.message import ToolCall
from devpilot.core.model import ModelResponse
from devpilot.runtime.lead_analysis import create_lead_analysis_app
from devpilot.tools.base import Tool
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.registry import ToolRegistry


class RecordingModel:
    """记录实际发给模型的工具和 Prompt，并按顺序调用真实工具。"""

    def __init__(self):
        """初始化工具调用轮次和执行记录。"""
        self.round = 0
        self.tools_seen = []
        self.systems = []
        self.received = []

    def complete(self, messages, tools=None, system=None):
        """使用固定工具调用证明整个 Agent Loop 的真实回传链路。"""
        self.round += 1
        self.tools_seen.append([t["function"]["name"] for t in tools or []])
        self.systems.append(system)
        self.received.append([(m.role, m.content) for m in messages])
        if self.round == 1:
            return ModelResponse("查询项目", [ToolCall("g1", "glob", {"pattern": "**/*.py"})])
        if self.round == 2:
            return ModelResponse("读取仓库", [ToolCall("r1", "read_file", {"path": "auth.py"})])
        if self.round == 3:
            return ModelResponse("写入任务", [ToolCall("t1", "task_create", {"title": "修复错误密码响应", "description": "确认 401"})])
        return ModelResponse("分析结束，已保存待办任务。")


@pytest.fixture
def repo(tmp_path):
    """构造一个包含 Python 源码和敏感文件的临时真实仓库目录。"""
    root = tmp_path / "repository"
    root.mkdir()
    (root / "auth.py").write_text("def login():\n    return 500\n", encoding="utf-8")
    (root / ".env").write_text("PRIVATE_TOKEN=should-not-appear", encoding="utf-8")
    (root / "secrets.json").write_text('{"password":"no"}', encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "skills" / "debugging").mkdir(parents=True)
    (root / "skills" / "debugging" / "SKILL.md").write_text("# Debugging\nCheck error paths", encoding="utf-8")
    return root


def test_end_to_end_real_lead_context_memory_taskgraph(repo):
    """真实调用 V0、V1、V2、V3，并验证任务落盘和旧工具输出回传模型。"""
    model = RecordingModel()
    with create_lead_analysis_app(repo, lambda _id: model) as app:
        app.core.lead.memory.remember("当前项目的登录模块在 auth.py", "project_fact")
        result = app.analyze("分析 auth.py 登录错误码")
        assert "分析结束" in result
        assert len(app.tasks()) == 1
        assert app.tasks()[0].status == "pending"
        assert "auth.py" in model.received[1][-1][1]
        assert "return 500" in model.received[2][-1][1]
        assert "Created task_" in model.received[3][-1][1]
        assert any("Persistent Memory" in prompt and "auth.py" in prompt for prompt in model.systems)
        assert any("Persistent Task Graph" in prompt for prompt in model.systems)
        assert all("bash" not in shown and "write_file" not in shown for shown in model.tools_seen)
        assert "read_file" in app.tools()
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as reopened:
        assert len(reopened.tasks()) == 1
        assert reopened.tasks()[0].title == "修复错误密码响应"
    assert (repo / "auth.py").read_text(encoding="utf-8") == "def login():\n    return 500\n"


def test_analysis_rejects_sensitive_file_and_outside_path(repo, tmp_path):
    """敏感文件、越界路径和隐藏运行目录不能通过受限读取工具泄露。"""
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as app:
        dispatcher = app.core.lead.dispatcher
        for path in (".env", "secrets.json", ".git/config", "../outside.py", ".devpilot/tasks.json"):
            result = dispatcher.execute_detailed(ToolCall("read", "read_file", {"path": path}))
            assert "Error:" in result.content
            assert "should-not-appear" not in result.content
        for pattern in ("../**/*.py", ".git/**/*", "**/*.py"):
            result = dispatcher.execute(ToolCall("glob", "glob", {"pattern": pattern}))
            assert "PRIVATE_TOKEN" not in result
        assert "auth.py" in dispatcher.execute(ToolCall("glob", "glob", {"pattern": "**/*.py"}))


def test_analysis_forbids_mutating_tools_even_if_model_invents_name(repo):
    """即使模型直接构造写入、Shell 或后台派发调用，也不能绕过工具注册白名单。"""
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as app:
        original = (repo / "auth.py").read_text(encoding="utf-8")
        for name, arguments in (("write_file", {"path": "auth.py", "content": "hacked"}),
                                ("bash", {"command": "echo bad > auth.py"}),
                                ("team_submit_ready", {}), ("task_update", {"task_id": "x", "status": "completed"})):
            result = app.core.lead.dispatcher.execute_detailed(ToolCall("no", name, arguments))
            assert result.status == "unknown_tool"
            assert result.ok is False
        assert (repo / "auth.py").read_text(encoding="utf-8") == original


def test_analysis_blocks_symlink_escape(repo, tmp_path):
    """拒绝通过符号链接把仓库外文件伪装成普通源码。"""
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'external'", encoding="utf-8")
    try:
        (repo / "alias.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Current filesystem does not permit symlink creation")
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as app:
        result = app.core.lead.dispatcher.execute(ToolCall("r", "read_file", {"path": "alias.py"}))
        assert "Error:" in result
        assert "external" not in result


def test_analysis_rejects_invalid_input_and_invalid_mode(repo):
    """拒绝空任务和不受支持的 Lead 模式，避免进入不清晰的运行状态。"""
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as app:
        with pytest.raises(ValueError):
            app.analyze("  ")
    from devpilot.runtime.factory import create_app
    with pytest.raises(ValueError):
        create_app(repo, lambda _id: RecordingModel(), lead_mode="not-valid")


def test_detailed_dispatcher_does_not_claim_text_return_is_success():
    """区分执行异常、权限拒绝与普通文本，不把字符串 Error 误报为业务成功。"""
    registry = ToolRegistry()
    registry.register(Tool("ordinary", "dummy", {"type": "object"}, lambda _: "Error: domain failure"))
    registry.register(Tool("explodes", "dummy", {"type": "object"}, lambda _: 1 / 0))
    hooks = HookManager()
    hooks.register_pre_tool_use(lambda call: "permission denied" if call.name == "ordinary" and call.arguments.get("block") else None)
    dispatcher = ToolDispatcher(registry, hooks)
    returned = dispatcher.execute_detailed(ToolCall("a", "ordinary", {}))
    assert returned.status == "returned" and returned.ok is None
    assert returned.content == "Error: domain failure"
    blocked = dispatcher.execute_detailed(ToolCall("b", "ordinary", {"block": True}))
    assert blocked.status == "denied" and blocked.ok is False
    assert dispatcher.execute_detailed(ToolCall("c", "missing", {})).status == "unknown_tool"
    failure = dispatcher.execute_detailed(ToolCall("d", "explodes", {}))
    assert failure.status == "exception" and "ZeroDivisionError" in failure.content
    assert dispatcher.execute(ToolCall("e", "ordinary", {})) == "Error: domain failure"


def test_skill_loading_is_constrained_to_real_file(repo):
    """Skill 工具可以读取合法 SKILL.md，但不允许通过任意路径加载敏感文件。"""
    with create_lead_analysis_app(repo, lambda _id: RecordingModel()) as app:
        dispatcher = app.core.lead.dispatcher
        assert "Check error paths" in dispatcher.execute(ToolCall("s", "load_skill", {"name": "debugging"}))
        assert "Error:" in dispatcher.execute(ToolCall("s", "load_skill", {"name": "../.env"}))


def test_model_tool_reply_is_not_claimed_success_for_denied(repo):
    """模型调用不在白名单中的工具时得到失败回执，但 Agent Loop 不会执行副作用。"""
    class NaughtyModel:
        def __init__(self):
            self.round = 0
        def complete(self, messages, tools=None, system=None):
            self.round += 1
            if self.round == 1:
                return ModelResponse("尝试编辑", [ToolCall("write", "write_file", {"path": "auth.py", "content": "bad"})])
            assert "unknown tool" in messages[-1].content
            return ModelResponse("不能写入代码，已停止")
    with create_lead_analysis_app(repo, lambda _id: NaughtyModel()) as app:
        assert "不能写入" in app.analyze("分析登录接口")
    assert "return 500" in (repo / "auth.py").read_text(encoding="utf-8")



def test_skill_catalog_does_not_expose_external_symlink(repo, tmp_path):
    """即使恶意 Skill 目录指向仓库外，也不会出现在模型系统提示词里。"""
    external = tmp_path / "external_skill"
    external.mkdir()
    (external / "SKILL.md").write_text("# Secret\nCREDENTIALS-NEVER-EXPOSE", encoding="utf-8")
    try:
        (repo / "skills" / "unsafe").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Current filesystem does not permit symlink creation")
    model = RecordingModel()
    with create_lead_analysis_app(repo, lambda _id: model) as app:
        app.analyze("分析登录接口")
        assert all("CREDENTIALS-NEVER-EXPOSE" not in prompt for prompt in model.systems)
        output = app.core.lead.dispatcher.execute(ToolCall("s", "load_skill", {"name": "unsafe"}))
        assert "Error:" in output
