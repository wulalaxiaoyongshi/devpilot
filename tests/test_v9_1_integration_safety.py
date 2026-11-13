"""V9.1：用真实 V1 和 V5 入口防止权限失效、任务通知丢失及生命周期错误。"""

from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest

from devpilot.background.manager import BackgroundJobManager
from devpilot.background.store import JobStore
from devpilot.core.message import ToolCall
from devpilot.runtime.composition import DevPilotComposition
from devpilot.runtime.permission import IntegratedPermission
from devpilot.tools.base import Tool
from devpilot.tools.dispatcher import ToolDispatcher
from devpilot.tools.hooks import HookManager
from devpilot.tools.permissions import PermissionPolicy
from devpilot.tools.registry import ToolRegistry


def call(name: str, **kwargs):
    """生成 V0 Agent 真正使用的 ToolCall，以便直接检查 V1 权限行为。"""
    return ToolCall("v9-call", name, kwargs)


def test_v1_write_requires_approval():
    """不允许未授权的文件写入工具在原生 V1 权限策略下通过。"""
    policy = PermissionPolicy(confirmer=lambda _: False)
    assert policy.check(call("write_file", path="a", content="x")) is not None
    assert policy.check(call("read_file", path="a")) is None
    assert policy.check(call("nonexistent_custom_tool")) is not None


def test_v1_dangerous_shell_is_denied_even_with_confirmation():
    """硬禁止命令不能通过允许回调获得执行权限。"""
    policy = PermissionPolicy(confirmer=lambda _: True)
    assert policy.check(call("bash", command="sudo reboot")) is not None
    assert policy.check(call("bash", command="rm -rf /")) is not None
    assert policy.check(call("bash", command="git status")) is None


def test_v1_shell_control_characters_cannot_use_safe_prefix():
    """以安全命令开头的 Shell 复合表达式仍然必须经过额外批准。"""
    policy = PermissionPolicy(confirmer=lambda _: False)
    assert policy.check(call("bash", command="pytest; echo unexpected")) is not None
    assert policy.check(call("bash", command="git status && echo unexpected")) is not None


def test_v5_requires_explicit_approval():
    """团队调度和取消操作没有可信审批时不能跳过原有权限链。"""
    policy = IntegratedPermission(PermissionPolicy(confirmer=lambda _: False))
    for name in ("team_spawn", "team_submit_ready", "job_cancel", "cron_create"):
        assert policy.check(call(name)) is not None
    assert policy.check(call("job_status")) is None


def test_mcp_permission_is_not_bypassed_by_integrated_wrapper():
    """真实 V1 Dispatcher 必须在 MCP 未授权时阻止工具 Handler 执行。"""
    from devpilot.mcp.permission import MCPPermissionPolicy
    allowed = {"value": False}
    count = {"value": 0}

    def action(args):
        """记录工具是否真的执行过，避免只检查提示文本而漏掉越权行为。"""
        count["value"] += 1
        return "tool-executed"

    registry = ToolRegistry()
    registry.register(Tool("mcp__demo__add", "test", {"type": "object"}, action))
    hooks = HookManager()
    policy = IntegratedPermission(
        MCPPermissionPolicy(PermissionPolicy(confirmer=lambda _: False),
                            approve=lambda name, args: allowed["value"]),
        approve=lambda name, args: allowed["value"],
    )
    hooks.register_pre_tool_use(policy.check)
    dispatcher = ToolDispatcher(registry, hooks)
    assert dispatcher.execute(call("mcp__demo__add")) != "tool-executed"
    assert count["value"] == 0
    allowed["value"] = True
    assert dispatcher.execute(call("mcp__demo__add")) == "tool-executed"
    assert count["value"] == 1


def test_factory_constructs_mcp_policy(tmp_path):
    """仓库实际 Factory 必须在 Lead 的 Hook 链中装配 MCP 的默认拒绝策略。"""
    from devpilot.runtime.factory import create_app
    from devpilot.core.model import MockModel
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    app = create_app(repo, lambda agent_id: MockModel(), state_dir=tmp_path / "state")
    try:
        hits = {"value": 0}

        def fake(args):
            """监听模拟 MCP Handler，证明拦截发生在真正调用之前。"""
            hits["value"] += 1
            return "allowed"

        app.lead.tools.register(Tool("mcp__demo__add", "test", {"type": "object"}, fake))
        assert app.lead.dispatcher.execute(call("mcp__demo__add")) != "allowed"
        assert hits["value"] == 0
        assert app.lead.dispatcher.execute(call("write_file", path="blocked.txt", content="x")) != "Wrote 1 characters to blocked.txt"
        assert not (repo / "blocked.txt").exists()
    finally:
        app.shutdown()


def test_background_complete_and_notice(tmp_path):
    """后台任务完成后必须实际调用完成通知并允许读取最终结果。"""
    notices = []
    manager = BackgroundJobManager(JobStore(tmp_path / "jobs.json"), on_finished=notices.append)
    try:
        job_id = manager.submit("ok", lambda context: "finished")
        assert manager.wait(job_id, timeout=5).status == "completed"
        assert manager.get_result(job_id) == "finished"
        assert [job.id for job in notices] == [job_id]
    finally:
        manager.shutdown()


def test_background_cancel_queued_job(tmp_path):
    """唯一线程被占用时能够取消排队 Job，并只通知一次取消事件。"""
    entered, release = Event(), Event()
    observed = []
    manager = BackgroundJobManager(JobStore(tmp_path / "jobs.json"), max_workers=1, on_finished=observed.append)

    def blocking(context):
        """占住唯一 Worker，直到测试显式发出完成信号。"""
        entered.set()
        release.wait(5)
        return "done"

    try:
        first = manager.submit("blocking", blocking)
        assert entered.wait(5)
        queued = manager.submit("queued", lambda context: "should-not-run")
        assert manager.cancel(queued) is True
        assert manager.wait(queued, timeout=2).status == "cancelled"
        assert manager.cancel(queued) is False
        release.set()
        assert manager.wait(first, timeout=5).status == "completed"
        assert [job.id for job in observed].count(queued) == 1
    finally:
        release.set()
        manager.shutdown()


@dataclass
class FakeCore:
    """提供最小真实契约以检查容器调用顺序而不启动其他 Agent。"""
    shutdown_count: int = 0

    def chat(self, text: str) -> str:
        """返回接收到的用户消息以确认入口代理正常。"""
        return text

    def pump(self, resume_lead: bool = True):
        """返回事件泵的调用参数供测试核对。"""
        return resume_lead

    def shutdown(self):
        """记录关闭次数以识别重复释放。"""
        self.shutdown_count += 1


class FakeWorkflow:
    """模拟具有关闭契约的 V7 应用，用于测试组合容器的归属与顺序。"""

    def __init__(self):
        """创建无需线程池的生命周期计数器。"""
        self.close_count = 0

    def close(self):
        """记录 Workflow 应用关闭次数。"""
        self.close_count += 1


class FakeGoal:
    """保留 V8 应用所属的工作流引用以检查配置不被交叉混用。"""

    def __init__(self, workflow):
        """绑定正确的 V7 应用对象。"""
        self.workflow = workflow


def test_composition_exposes_only_connected_capabilities():
    """没有真实绑定的 V7/V8 不得在统一入口里报告为可用。"""
    core = FakeCore()
    with DevPilotComposition(core) as app:
        assert app.chat("hello") == "hello"
        assert app.pump(False) is False
        assert app.capabilities() == {
            "lead_v0_to_v5": True, "mcp_v6": False, "workflow_v7": False, "goal_v8": False
        }
    assert core.shutdown_count == 1
    app.close()
    assert core.shutdown_count == 1


def test_composition_checks_goal_workflow_ownership():
    """V8 Goal 使用的 V7 工作流与容器传入的不一致时必须拒绝组装。"""
    core = FakeCore()
    wf = FakeWorkflow()
    with pytest.raises(ValueError):
        DevPilotComposition(core, workflow=wf, goal=FakeGoal(FakeWorkflow()))
    with DevPilotComposition(core, workflow=wf, goal=FakeGoal(wf)) as app:
        assert app.capabilities()["goal_v8"] is True
    assert wf.close_count == 1
    assert core.shutdown_count == 1
