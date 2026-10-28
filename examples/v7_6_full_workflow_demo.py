"""在真实 Git Worktree 上运行 V7 Coding Workflow，其他版本使用显式演示适配器。"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from devpilot.workflow import (
    ExternalOutcome, WorkflowApplication, WorkflowBindings,
    build_coding_workflow, register_coding_verification,
)


class CodingDemoPorts:
    """提供真实 Git 与 unittest、内存任务表和模拟 MCP 结果，供无 API Key 的端到端示例运行。"""

    def __init__(self, directory: Path):
        """初始化临时 Git 仓库、两个隔离 Worktree 和执行测试的后台线程池。"""
        self.directory = Path(directory).resolve()
        self.repo = self.directory / "repo"
        self.repo.mkdir(parents=True)
        self._git(self.repo, "init", "-q", "-b", "main")
        self._git(self.repo, "config", "user.name", "DevPilot Demo")
        self._git(self.repo, "config", "user.email", "devpilot-demo@example.invalid")
        (self.repo / "README.md").write_text("示例：用两个 Worker 完成加法模块与测试。\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-qm", "initial commit")
        self.worktrees: dict[str, Path] = {}
        for role in ("backend", "qa"):
            path = self.directory / "trees" / role
            path.parent.mkdir(exist_ok=True, parents=True)
            self._git(self.repo, "worktree", "add", "-q", "-b", f"worker-{role}", str(path))
            self.worktrees[role] = path
        self.tasks: dict[str, dict] = {}
        self.jobs: dict[str, Future[dict]] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="demo-unittest")
        self._lock = Lock()

    @staticmethod
    def _git(directory: Path, *args: str) -> str:
        """使用参数列表运行真实 Git 并在失败时保留退出信息。"""
        result = subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, check=True, timeout=15)
        return result.stdout.strip()

    def dispatch_tool(self, name: str, arguments: dict) -> dict:
        """模拟已通过 V1 权限的读写工具和 V6 MCP 查询，并限制真实文件副作用的路径。"""
        if name == "read_file" and arguments.get("path") == "README.md":
            return {"ok": True, "content": {"text": (self.repo / "README.md").read_text(encoding="utf-8")}}
        if name == "write_file" and arguments.get("path") == "DELIVERED.txt":
            (self.repo / "DELIVERED.txt").write_text(str(arguments.get("content", "")), encoding="utf-8")
            return {"ok": True, "content": {"path": "DELIVERED.txt"}}
        if name == "mcp__docs__search" and isinstance(arguments.get("q"), str):
            return {"ok": True, "content": {"snippet": "MCP 查询示例；此处没有连接真实 MCP Server"}}
        return {"ok": False, "content": "演示宿主拒绝未经授权的工具或路径"}

    def create_task(self, payload: dict) -> str:
        """在演示内存 TaskStore 中创建唯一 Task ID，代表 V3 的持久化创建接口。"""
        task_id = "task-" + uuid4().hex[:8]
        with self._lock:
            self.tasks[task_id] = payload
        return task_id

    def run_assignment(self, task_id: str, agent_id: str) -> dict:
        """代替真实 V4 TeamManager，在指定 Git Worktree 写代码并完成真实 Git Commit。"""
        if task_id not in self.tasks or agent_id not in self.worktrees:
            return {"ok": False, "error": "Unknown task or worker"}
        worktree = self.worktrees[agent_id]
        if agent_id == "backend":
            (worktree / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            self._git(worktree, "add", "calculator.py")
        else:
            (worktree / "tests").mkdir(exist_ok=True)
            (worktree / "tests" / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 5), 7)\n", encoding="utf-8"
            )
            self._git(worktree, "add", "tests/test_calculator.py")
        self._git(worktree, "commit", "-qm", f"implement {agent_id}")
        return {"ok": True, "commit": self._git(worktree, "rev-parse", "HEAD"), "branch": f"worker-{agent_id}"}

    def merge_worktrees(self, ctx) -> dict:
        """只有后台和测试分支都提交成功时才在主 Git 仓库执行真实合并。"""
        branches = (ctx.results["backend"]["result"]["branch"], ctx.results["qa"]["result"]["branch"])
        commits = []
        for branch in branches:
            self._git(self.repo, "merge", "--no-ff", "--no-edit", branch)
            commits.append(self._git(self.repo, "rev-parse", "HEAD"))
        return {"merged": True, "commits": commits}

    def _run_unittest(self) -> dict:
        """在合并后的真实主仓库执行 Python unittest，并收集真实退出码和输出。"""
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                cwd=self.repo, capture_output=True, text=True, timeout=25)
        return {"passed": result.returncode == 0, "exit_code": result.returncode,
                "log": (result.stdout + result.stderr)[-2500:]}

    def submit_job(self, job_type: str, payload: dict) -> str:
        """使用演示线程池提交真正的 unittest 子进程并返回 Job ID，但不冒充 V5 JobStore。"""
        if job_type != "pytest":
            raise ValueError("Only the workflow test job is available in this demo")
        job_id = "job-" + uuid4().hex[:8]
        with self._lock:
            self.jobs[job_id] = self._pool.submit(self._run_unittest)
        return job_id

    def query_job(self, job_id: str) -> ExternalOutcome:
        """根据真实后台 Future 的运行状态返回 ExternalOutcome，供 Workflow 判断是否能推进。"""
        with self._lock:
            future = self.jobs.get(job_id)
        if future is None:
            return ExternalOutcome("unknown")
        if not future.done():
            return ExternalOutcome("running")
        try:
            result = future.result()
        except Exception as error:
            return ExternalOutcome("failed", error=str(error))
        if not result["passed"]:
            return ExternalOutcome("failed", output=result, error="Unit tests failed")
        return ExternalOutcome("succeeded", output=result)

    def bindings(self) -> WorkflowBindings:
        """将演示宿主的各执行入口装配成 V7.6 所需的明确注入契约。"""
        return WorkflowBindings(self.dispatch_tool, self.create_task, self.run_assignment,
                                self.submit_job, self.query_job, self.merge_worktrees,
                                frozenset({"read_file", "write_file"}), frozenset({"mcp__docs__search"}))

    def close(self) -> None:
        """等待演示后台测试结束并释放线程资源。"""
        self._pool.shutdown(wait=True, cancel_futures=False)


def run_demo(root: Path) -> tuple[str, list[str]]:
    """运行真实 Git 并行编码、合并、后台测试、人工审批和交付标记的完整工作流。"""
    ports = CodingDemoPorts(root)
    observed: list[str] = []
    try:
        with WorkflowApplication(root / "state", ports.bindings(), max_workers=3) as app:
            register_coding_verification(app.actions)
            run = app.start(build_coding_workflow(), {"input_file": "README.md", "query": "python unittest"})
            run = app.run_until_blocked(run.id, timeout_seconds=8)
            observed.append(f"第一阶段：{run.status}")
            assert run.status == "waiting_external", run.status
            assert run.steps["review"].status == "pending"
            while run.status == "waiting_external":
                from time import sleep
                sleep(0.02)
                run = app.tick(run.id, poll_external=True)
            run = app.run_until_blocked(run.id, timeout_seconds=8)
            observed.append(f"第二阶段：{run.status}")
            assert run.status == "waiting_approval", run.status
            assert not (ports.repo / "DELIVERED.txt").exists()
            request = app.pending_approvals(run.id)[0]
            app.approve(run.id, request.step_id, actor="reviewer", request_id=request.request_id,
                        approved=True, note="核实真实 Git 合并与 unittest 输出")
            run = app.run_until_blocked(run.id, timeout_seconds=8)
            observed.append(f"最终：{run.status}")
            assert run.status == "completed", run.status
            assert (ports.repo / "DELIVERED.txt").is_file()
            return run.id, observed
    finally:
        ports.close()


def main() -> None:
    """在临时目录执行 V7.6 演示并打印每个关键关卡的状态。"""
    with TemporaryDirectory(prefix="devpilot-v76-") as folder:
        run_id, observed = run_demo(Path(folder))
        print("[Workflow Run ID]", run_id)
        for item in observed:
            print(" -", item)
        print("[验证] 已通过真实 Git Worktree、Git Merge、unittest、审批与交付检查")
        print("[边界] V1/V3/V5/V6 仍是演示端口；完整原工程对接需在你的项目执行")


if __name__ == "__main__":
    main()
