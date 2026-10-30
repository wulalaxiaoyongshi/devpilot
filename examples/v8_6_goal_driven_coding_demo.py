"""以真实 Git 登录 Bug 和 unittest 展示 V8 失败修复、V7 人工审批及证据交付闭环。"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from devpilot.goal import (
    AcceptanceCriterion, GoalApplication, GoalBudget, GoalDeliveryBuilder,
    GoalReportStore, GoalSession, GoalSpec,
)
from devpilot.workflow import (
    ApprovalPolicy, StepDefinition, WorkflowApplication, WorkflowBindings, WorkflowDefinition,
)
from devpilot.workflow.external import ExternalOutcome


def git(repo: Path, *args: str) -> str:
    """只在本示例的临时仓库执行真实 Git 命令。"""
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    """创建包含真实登录错误和回归测试的最小 Git 项目。"""
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "auth.py").write_text("def login(password):\n    return 200 if password == 'secret' else 500\n", encoding="utf-8")
    (repo / "tests" / "test_login.py").write_text(
        "import unittest\nfrom auth import login\n\nclass TestLogin(unittest.TestCase):\n"
        "    def test_wrong_password(self):\n        self.assertEqual(login('bad'), 401)\n\n"
        "    def test_valid_password(self):\n        self.assertEqual(login('secret'), 200)\n", encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "auth.py", "tests/test_login.py", ".gitignore")
    git(repo, "-c", "user.name=DevPilot", "-c", "user.email=devpilot@example.invalid", "commit", "-qm", "buggy-login")
    return repo


def demo_bindings() -> WorkflowBindings:
    """提供明确标记为演示替身的 V1/V3/V4/V5/V6 端口，真实 V7 和 Git 仍运行。"""
    return WorkflowBindings(
        dispatch_tool=lambda name, args: {"tool": name, "args": args},
        create_task=lambda data: "demo-task",
        run_assignment=lambda worker, task: {"worker": worker, "task": task},
        submit_job=lambda name, args: "demo-job",
        query_job=lambda job_id: ExternalOutcome("succeeded", output={"job_id": job_id}),
        merge_worktrees=lambda ctx: {"merge": "not used"},
        allowed_tools=frozenset({"file.read"}),
        allowed_mcp_tools=frozenset({"mcp__demo__read"}),
    )


def run_demo(root: Path) -> dict:
    """真实重现一次 Bug 修复成功、审批拦截、报告生成和验收过期。"""
    if (root / "repo").exists():
        raise FileExistsError("演示目录下已有 repo，拒绝覆盖现有 Git 数据")
    repo = make_repo(root)
    state = root / "state"
    cmd = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    with WorkflowApplication(state / "workflows", demo_bindings()) as workflow:
        def fix_login(ctx):
            """修改真实仓库的登录错误并通过 Git Commit 保存修复。"""
            (repo / "auth.py").write_text("def login(password):\n    return 200 if password == 'secret' else 401  # corrected\n", encoding="utf-8")
            git(repo, "add", "auth.py")
            git(repo, "-c", "user.name=DevPilot", "-c", "user.email=devpilot@example.invalid", "commit", "-qm", "fix-wrong-password")
            return {"commit": git(repo, "rev-parse", "HEAD")}

        def no_change(ctx):
            """故意不修改任何文件，以验证预算耗尽后不会再启动修复。"""
            return {"changed": False}

        workflow.actions.register("demo.fix_login", fix_login)
        workflow.actions.register("demo.no_change", no_change)

        def trusted_plan(feedback):
            """根据受信任的 Goal 类型返回预定义工作流，不执行模型反馈字符串。"""
            if feedback.goal_title == "预算耗尽演示":
                return WorkflowDefinition("no-change", "安全演示", (StepDefinition("noop", action="demo.no_change"),))
            return WorkflowDefinition("repair-login", "修复登录", (
                StepDefinition("fix", action="demo.fix_login"),
                StepDefinition("review", kind="approval", depends_on=("fix",),
                               approval=ApprovalPolicy(("reviewer",), requester="agent",
                                                       purpose="检查登录修复和 Git 变更后批准继续")),
            ))

        app = GoalApplication(state, repo, workflow, trusted_plan, allowed_commands=(cmd,))
        session = GoalSession(app)
        spec = GoalSpec("login-issue", "登录 Bug 修复", "密码错误时返回 401，合法密码仍返回 200，回归测试通过", (
            AcceptanceCriterion("correct-code", "登录实现必须返回 401", "file_contains", {"path": "auth.py", "contains": "else 401"}),
            AcceptanceCriterion("regression", "两个登录回归测试都通过", "command_exit_code", {"argv": list(cmd), "expected_exit_code": 0}),
            AcceptanceCriterion("tests-exist", "回归测试文件存在", "file_exists", {"path": "tests/test_login.py"}),
        ))
        run = app.start(spec, GoalBudget(max_rounds=2, max_elapsed_seconds=600, max_tokens=5000))
        app.begin_execution(run.id)
        app.begin_verification(run.id)
        first = session.run_until_boundary(run.id)
        assert first[-1].action == "waiting_approval", first
        assert app.get(run.id).status == "repairing"
        pre_report = GoalDeliveryBuilder(app).build(run.id)
        assert pre_report.delivery_status == "not_verified"
        inbox = workflow.pending_approvals(first[-1].workflow_run_id)
        assert len(inbox) == 1
        request = inbox[0]
        workflow.approve(request.run_id, request.step_id, actor="reviewer", request_id=request.request_id,
                         approved=True, note="已确认真实 Git Commit 和回归测试配置")
        updates = session.run_until_boundary(run.id)
        assert app.get(run.id).status == "verified", updates
        reports = GoalReportStore(state / "reports")
        builder = GoalDeliveryBuilder(app)
        final_report = builder.build(run.id)
        assert final_report.delivery_status == "ready_for_delivery"
        json_path, md_path = reports.append(final_report)
        assert reports.get(run.id, final_report.id) == final_report

        (repo / "auth.py").write_text("# 未提交的额外改动\n" + (repo / "auth.py").read_text(encoding="utf-8"), encoding="utf-8")
        stale = builder.build(run.id)
        assert stale.delivery_status == "verification_stale"
        reports.append(stale)
        git(repo, "restore", "auth.py")

        exhausted_spec = GoalSpec("exhaust", "预算耗尽演示", "必须存在一个本例永远不会创建的文件", (
            AcceptanceCriterion("missing", "文件 never_created.py 必须存在", "file_exists", {"path": "never_created.py"}),
        ))
        exhausted_run = app.start(exhausted_spec, GoalBudget(max_rounds=1))
        app.begin_execution(exhausted_run.id)
        app.begin_verification(exhausted_run.id)
        budget_updates = session.run_until_boundary(exhausted_run.id)
        assert app.get(exhausted_run.id).status == "exhausted", budget_updates
        budget_report = builder.build(exhausted_run.id)
        assert budget_report.delivery_status == "not_verified"
        reports.append(budget_report)
        summary = {
            "goal_id": run.id,
            "first_boundary": first[-1].action,
            "reviewer": final_report.reviewer,
            "final_goal": final_report.goal_status,
            "delivery": final_report.delivery_status,
            "git_head": final_report.git_head,
            "evidence_count": len(app.evidence_store.list_for_goal(run.id)),
            "repair_rounds": final_report.used_rounds,
            "json_report": str(json_path),
            "markdown_report": str(md_path),
            "dirty_worktree_delivery": stale.delivery_status,
            "budget_goal": app.get(exhausted_run.id).status,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary


def main() -> None:
    """默认使用临时仓库，也允许指定独立输出目录保存所有 Git 和验收报告。"""
    parser = argparse.ArgumentParser(description="DevPilot V8.6 Goal-Driven Coding Demo")
    parser.add_argument("--output", type=Path, default=None, help="保存独立演示仓库、状态和报告的目录")
    args = parser.parse_args()
    if args.output is not None:
        root = args.output.resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_demo(root)
        print("报告已保存到：", root / "state" / "reports")
    else:
        with tempfile.TemporaryDirectory(prefix="devpilot-v86-") as directory:
            run_demo(Path(directory))
        print("注意：默认演示使用临时目录，退出后删除；使用 --output 可以保留。")
    print("注意：V0～V6 仍使用宿主演示端口；V7 Workflow、Git、unittest、V8 Goal 均为真实运行。")


if __name__ == "__main__":
    main()
