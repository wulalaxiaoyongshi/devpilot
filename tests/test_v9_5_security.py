"""V9.5：针对 MCP 动态授权、Workflow 计划审查及非幂等操作防重放的专项测试。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot.core.message import ToolCall
from devpilot.runtime.mcp_access import MCPAccessController, MCPServerAccess
from devpilot.runtime.operation_guard import OperationLedger, protect_workflow_action
from devpilot.runtime.plan_guard import PlanActionRule, WorkflowPlanGuard
from devpilot.workflow.engine import WorkflowEngine
from devpilot.workflow.actions import ActionRegistry
from devpilot.workflow.model import StepContext, StepDefinition, WorkflowDefinition
from devpilot.workflow.store import WorkflowStore
from devpilot.workflow.recovery import UncertainActionError
from examples.v9_4_full_chain_acceptance import OfflineCodingModel, OfflineIssueBridge, create_fixture
from devpilot.runtime.full_chain import V94Integration


class FakeMCPManager:
    """模拟真实管理器的公开状态接口而不伪造权限通过的远端调用。"""

    def __init__(self):
        """初始化处于连接状态且仅暴露两种动作的服务器。"""
        self.generation = 1
        self.state = "connected"
        self.tools = ("mcp__github__get_issue", "mcp__github__delete_repo")

    def status(self, alias):
        """读取指定 Server 的连接代数及已发现工具。"""
        assert alias == "github"
        return SimpleNamespace(generation=self.generation, state=self.state, tools=self.tools)

    def statuses(self):
        """返回管理器上报的全体连接信息。"""
        return {"github": self.status("github")}


def plan_guard():
    """创建只允许读取和修改单个文本参数的可信计划审查器。"""
    return WorkflowPlanGuard((PlanActionRule("inspect", frozenset({"path"})),
                              PlanActionRule("edit", frozenset({"path"}), mutates=True)))


def step(step_id, action, depends=(), params=None):
    """为测试准备符合模型计划输入形式的简短步骤。"""
    return {"id": step_id, "action": action, "depends_on": list(depends), "params": params or {}}


def test_dynamic_read_from_discovery_without_tool_name_allowlist():
    """可信宿主只授权服务端级读取时可使用动态发现的新查询工具。"""
    manager = FakeMCPManager()
    policy = MCPAccessController((MCPServerAccess("github", auto_read=True),))
    policy.sync_discovery(manager)
    assert policy.authorize("mcp__github__get_issue", {"number": 15})
    assert not policy.authorize("mcp__github__delete_repo", {})


def test_permission_denies_unseen_server_and_unlisted_name():
    """未知 Server、未发现工具和未同步的工具都不能直接调用。"""
    policy = MCPAccessController((MCPServerAccess("github", auto_read=True),))
    assert not policy.authorize("mcp__github__get_issue", {})
    policy.sync_discovery(FakeMCPManager())
    assert not policy.authorize("mcp__github__list_secrets", {})
    assert not policy.authorize("mcp__other__get_issue", {})


def test_generation_changed_requires_explicit_refresh():
    """重连导致连接代数变化时旧工具授权自动失效。"""
    manager = FakeMCPManager()
    policy = MCPAccessController((MCPServerAccess("github", auto_read=True),))
    policy.sync_discovery(manager)
    manager.generation += 1
    assert not policy.authorize("mcp__github__get_issue", {})
    policy.sync_discovery(manager)
    assert policy.authorize("mcp__github__get_issue", {})
    manager.state = "degraded"
    assert not policy.authorize("mcp__github__get_issue", {})


def test_read_name_not_enough_for_dangerous_tool_or_args():
    """可疑操作名、Shell 参数与过大请求即使处于可信服务端也必须拒绝。"""
    manager = FakeMCPManager()
    manager.tools += ("mcp__github__get_delete_repo", "mcp__github__search_issues")
    policy = MCPAccessController((MCPServerAccess("github", auto_read=True, max_argument_bytes=64),))
    policy.sync_discovery(manager)
    assert not policy.authorize("mcp__github__get_delete_repo", {})
    assert not policy.authorize("mcp__github__search_issues", {"command": "rm -rf /"})
    assert not policy.authorize("mcp__github__search_issues", {"query": "x" * 200})


def test_mutation_requires_host_callback_exactly_true():
    """写操作只有宿主打开写权限并确认本次参数时才可执行。"""
    policy = MCPAccessController((MCPServerAccess("github", allow_reviewed_mutations=True),),
                                 approve_mutation=lambda name, args: name == "mcp__github__delete_repo" and args == {"repo": "test"})
    policy.sync_discovery(FakeMCPManager())
    assert policy.authorize("mcp__github__delete_repo", {"repo": "test"})
    assert not policy.authorize("mcp__github__delete_repo", {"repo": "prod"})


def test_mutation_without_host_callback_is_denied():
    """配置了可审批写入但没有真实宿主审批函数时仍必须拒绝。"""
    policy = MCPAccessController((MCPServerAccess("github", allow_reviewed_mutations=True),))
    policy.sync_discovery(FakeMCPManager())
    assert not policy.authorize("mcp__github__delete_repo", {})


def test_plan_guard_accepts_read_only_dag_without_approval():
    """合法的只读任务图可以直接进入 V7 原有工作流定义校验器。"""
    workflow = plan_guard().compile({"steps": [step("a", "inspect", params={"path": "a.py"}),
                                             step("b", "inspect", ("a",), {"path": "b.py"})]}, workflow_id="read_plan")
    assert len(workflow.steps) == 2
    assert workflow.steps[0].kind == "action"


def test_mutation_plan_is_blocked_until_real_v7_approval(tmp_path):
    """模型计划含副作用时最先打开真实 V7 人工审批而不会抢先执行动作。"""
    workflow = plan_guard().compile({"steps": [step("a", "inspect", params={"path": "a.py"}),
                                             step("b", "edit", ("a",), {"path": "a.py"})]}, workflow_id="write_plan")
    seen = []
    actions = ActionRegistry()
    actions.register("inspect", lambda c: seen.append("inspect") or {"ok": True})
    actions.register("edit", lambda c: seen.append("edit") or {"ok": True})
    engine = WorkflowEngine(WorkflowStore(tmp_path), actions)
    run = engine.start(workflow)
    assert engine.run_until_blocked(run.id).status == "waiting_approval"
    assert seen == []
    request = engine.get_approval_request(run.id, "preapproval")
    engine.approve_step(run.id, "preapproval", True, actor="reviewer", request_id=request.request_id)
    assert engine.run_until_blocked(run.id).status == "completed"
    assert seen == ["inspect", "edit"]


def test_plan_rejects_unknown_action_extra_keys_cycle_and_privilege():
    """未知动作、多余字段、依赖环和越权参数都会在执行前被拒绝。"""
    guard = plan_guard()
    with pytest.raises(ValueError):
        guard.compile({"steps": [step("a", "shell")]}, workflow_id="bad")
    with pytest.raises(ValueError):
        guard.compile({"steps": [step("a", "inspect", ("b",)), step("b", "inspect", ("a",))]}, workflow_id="bad")
    with pytest.raises(PermissionError):
        guard.compile({"steps": [step("a", "inspect", params={"command": "whoami"})]}, workflow_id="bad")
    with pytest.raises(ValueError):
        guard.compile({"steps": [dict(step("a", "inspect"), kind="approval")]}, workflow_id="bad")


def test_plan_rejects_excessive_steps_and_bad_json():
    """模型输出的过长计划与不可 JSON 序列化参数都不会被执行。"""
    guard = WorkflowPlanGuard((PlanActionRule("read", frozenset()),), max_steps=1)
    with pytest.raises(ValueError):
        guard.compile({"steps": [step("a", "read"), step("b", "read")]}, workflow_id="bad")
    with pytest.raises(ValueError):
        guard.compile({"steps": [step("a", "read", params={"x": object()})]}, workflow_id="bad")


def test_ledger_refuses_second_submit_even_after_restart(tmp_path):
    """操作意图持久化以后即使重启创建新账本实例也不能重复提交。"""
    ledger = OperationLedger(tmp_path)
    ledger.begin("run:a", description="Create Issue")
    with pytest.raises(UncertainActionError):
        OperationLedger(tmp_path).begin("run:a", description="Create Issue")
    assert ledger.get("run:a")["status"] == "inflight"


def test_ledger_requires_evidence_for_reconciliation(tmp_path):
    """结果未知不能由空用户名或空证据直接标记为成功。"""
    ledger = OperationLedger(tmp_path)
    ledger.begin("run:a", description="Create Issue")
    with pytest.raises(ValueError):
        ledger.reconcile("run:a", status="succeeded", actor="", evidence="")
    ledger.reconcile("run:a", status="succeeded", actor="reviewer", evidence="remote issue 123 exists")
    assert OperationLedger(tmp_path).get("run:a")["status"] == "succeeded"
    with pytest.raises(UncertainActionError):
        ledger.begin("run:a", description="Duplicate")


def test_guarded_action_uncertain_error_triggers_v7_recovery(tmp_path):
    """有副作用的动作失败后应进入 V7 恢复核实状态而不是默认重试。"""
    ledger = OperationLedger(tmp_path / "ops")
    seen = []
    actions = ActionRegistry()
    def uncertain(_ctx):
        """模拟外部调用已执行但连接断开的情况。"""
        seen.append(1)
        raise TimeoutError("response lost")
    actions.register("unsafe", protect_workflow_action(ledger, uncertain))
    engine = WorkflowEngine(WorkflowStore(tmp_path / "workflow"), actions)
    run = engine.start(WorkflowDefinition("unsafe_run", "unknown side effect", (StepDefinition("write", action="unsafe"),)))
    assert engine.run_until_blocked(run.id).status == "recovery_required"
    assert seen == [1]
    assert ledger.get(f"{run.id}:write")["status"] == "inflight"
    assert engine.run_until_blocked(run.id).status == "recovery_required"
    assert seen == [1]


def test_real_v9_4_chain_uses_dynamic_policy_for_lead(tmp_path):
    """在真实 V9.4 组合入口下授权已连接 MCP 读工具并拒绝未知写工具。"""
    repo = create_fixture(tmp_path / "repo")
    policy = MCPAccessController((MCPServerAccess("fixture", auto_read=True),))
    with V94Integration(repo, tmp_path / "state", lambda name: OfflineCodingModel(name),
                        mcp_bridge_factory=lambda d: OfflineIssueBridge(), mcp_authorizer=policy) as app:
        assert policy.authorize("mcp__fixture__get_issue", {"issue_number": 15})
        assert not policy.authorize("mcp__fixture__delete_repo", {})
        result = app._dispatch_tool("mcp__fixture__get_issue", {"issue_number": 15})
        assert result["ok"]
        denied = app.team.core.lead.dispatcher.execute_detailed(ToolCall("deny", "mcp__fixture__delete_repo", {}))
        assert denied.status == "unknown_tool"


def test_model_parameter_value_needs_host_validator():
    """即使动作和参数名称已授权，也不能让模型选择越权目标文件。"""
    guard = WorkflowPlanGuard((PlanActionRule("read", frozenset({"path"}),
                                                   param_validators={"path": lambda value: value == "auth.py"}),))
    with pytest.raises(PermissionError):
        guard.compile({"steps": [step("a", "read", params={"path": ".env"})]}, workflow_id="bad")
    assert guard.compile({"steps": [step("a", "read", params={"path": "auth.py"})]}, workflow_id="good").steps[0].id == "a"
