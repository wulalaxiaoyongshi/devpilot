"""无需 LLM 或 API Key 的 V8.1 登录 Bug 目标定义与持久化演示。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from devpilot.goal import AcceptanceCriterion, GoalRuntime, GoalSpec, GoalStore


def build_login_goal() -> GoalSpec:
    """为登录错误码修复创建三条必须满足的明确验收条件。"""
    return GoalSpec(
        id="fix-login-401",
        title="修复登录错误码",
        description="密码错误时返回 401，正常登录和全部回归测试不能被破坏。",
        criteria=(
            AcceptanceCriterion(
                id="invalid_password", description="错误密码时响应必须返回 401",
                kind="command_exit_code",
                config={"argv": ["python", "-m", "pytest", "tests/test_auth.py", "-k", "invalid_password", "-q"]},
            ),
            AcceptanceCriterion(
                id="valid_password", description="有效密码仍然可以登录",
                kind="command_exit_code",
                config={"argv": ["python", "-m", "pytest", "tests/test_auth.py", "-k", "valid_password", "-q"]},
            ),
            AcceptanceCriterion(
                id="regression", description="项目回归测试通过",
                kind="command_exit_code",
                config={"argv": ["python", "-m", "pytest", "-q"]},
            ),
        ),
    )


def main() -> None:
    """创建目标、模拟 Workflow 开始、重启读取并展示尚未验证的验收条件。"""
    with TemporaryDirectory(prefix="devpilot-v8-1-") as temporary:
        root = Path(temporary) / ".devpilot" / "goals"
        runtime = GoalRuntime(GoalStore(root))
        run = runtime.start(build_login_goal(), {"issue": "密码错误返回了 500"})
        print("[创建 Goal]", run.id, run.status)
        run = runtime.begin_execution(run.id)
        print("[Workflow 执行中]", run.status)
        run = runtime.begin_verification(run.id)
        print("[等待 V8.3 验收]", run.status)
        restarted = GoalRuntime(GoalStore(root))
        saved = restarted.get(run.id)
        print("[重启后读取]", saved.spec.title, "版本", saved.version)
        for item in saved.spec.criteria:
            print("  验收条件：", item.id, "→", saved.criteria[item.id].status)
        print("[说明] V8.1 不会执行 pytest，也不会把目标标成 verified。")


if __name__ == "__main__":
    main()
