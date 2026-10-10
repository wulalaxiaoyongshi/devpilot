import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from devpilot.agents.mailbox import MailboxStore
from devpilot.agents.subagent import SubagentRunner
from devpilot.agents.team import TeamManager
from devpilot.tasks.graph import TaskGraph
from devpilot.tasks.ownership import TaskOwnership
from devpilot.tasks.store import TaskStore
from devpilot.workspace.worktree import WorktreeManager


# ============================================================
# Test helpers
# ============================================================


def run_git(
    cwd: Path,
    *args: str,
) -> str:

    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )

    return result.stdout.strip()


def create_git_repo(
    root: Path,
) -> Path:

    repo = root / "repo"

    repo.mkdir()

    run_git(
        repo,
        "init",
    )

    # 只配置当前测试仓库，
    # 不修改你电脑的全局 Git 配置。
    run_git(
        repo,
        "config",
        "user.name",
        "DevPilot Test",
    )

    run_git(
        repo,
        "config",
        "user.email",
        "devpilot-test@example.com",
    )

    (
        repo / "README.md"
    ).write_text(
        "# DevPilot V4 Test\n",
        encoding="utf-8",
    )

    run_git(
        repo,
        "add",
        ".",
    )

    run_git(
        repo,
        "commit",
        "-m",
        "initial commit",
    )

    return repo


# ============================================================
# Fake Agent for Subagent
# ============================================================


class FreshFakeAgent:

    def __init__(self):
        self.messages = []

    def run(
        self,
        prompt: str,
    ) -> str:

        self.messages.append(
            prompt
        )

        return (
            f"messages="
            f"{len(self.messages)}"
        )


# ============================================================
# Fake persistent teammate
# ============================================================


class FakeWorkerAgent:

    def __init__(
        self,
        workdir: Path,
        agent_id: str,
        first_batch_barrier: Barrier,
    ):
        self.workdir = workdir
        self.agent_id = agent_id

        self.barrier = (
            first_batch_barrier
        )

        self.calls = 0

        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
    ) -> str:

        self.calls += 1

        self.prompts.append(
            prompt
        )

        # --------------------------------------
        # 第一轮要求两个 Worker 必须同时到这里
        #
        # 如果 TeamManager 是串行执行，
        # 第一个 Worker 会一直等第二个，
        # 测试直接失败。
        #
        # 因此这是真正在测试并行。
        # --------------------------------------

        if self.calls == 1:

            self.barrier.wait(
                timeout=5
            )

        # --------------------------------------
        # 模拟 Worker 真正修改代码
        # --------------------------------------

        target = (
            self.workdir
            / f"{self.agent_id}.txt"
        )

        previous = ""

        if target.exists():

            previous = (
                target.read_text(
                    encoding="utf-8"
                )
            )

        target.write_text(
            previous
            + (
                f"{self.agent_id} "
                f"call={self.calls}\n"
            ),
            encoding="utf-8",
        )

        return (
            f"{self.agent_id} "
            f"finished call "
            f"{self.calls}"
        )


# ============================================================
# V4 full integration test
# ============================================================


def test_v4_full_runtime(
    tmp_path: Path,
):

    # ========================================================
    # PART 1
    # Fresh-context Subagent
    # ========================================================

    created_subagents = []

    def subagent_factory(
        workdir: Path,
        agent_id: str,
    ):

        agent = FreshFakeAgent()

        created_subagents.append(
            agent
        )

        return agent

    subagent_runner = (
        SubagentRunner(
            subagent_factory
        )
    )

    result_a = (
        subagent_runner.run(
            agent_id="subagent_a",
            workdir=tmp_path,
            task="Inspect backend",
        )
    )

    result_b = (
        subagent_runner.run(
            agent_id="subagent_b",
            workdir=tmp_path,
            task="Inspect tests",
        )
    )

    # 每次 Subagent 都应该是新 Agent
    assert (
        result_a.output
        == "messages=1"
    )

    assert (
        result_b.output
        == "messages=1"
    )

    assert (
        created_subagents[0]
        is not
        created_subagents[1]
    )

    # ========================================================
    # PART 2
    # Task ownership concurrency
    # ========================================================

    race_store = TaskStore(
        tmp_path
        / "race_tasks.json"
    )

    race_task = (
        race_store.create(
            title=(
                "Only one worker "
                "may claim this"
            )
        )
    )

    race_graph = TaskGraph(
        race_store
    )

    ownership = TaskOwnership(
        store=race_store,
        graph=race_graph,
    )

    def try_claim(
        worker_id: str,
    ) -> str | None:

        try:

            ownership.claim(
                task_id=race_task.id,
                owner=worker_id,
            )

            return worker_id

        except ValueError:

            return None

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:

        claim_results = list(
            executor.map(
                try_claim,
                [
                    "worker_a",
                    "worker_b",
                ],
            )
        )

    winners = [
        result
        for result
        in claim_results
        if result is not None
    ]

    # 两个 Worker 同时抢同一个 Task，
    # 只能一个成功。
    assert len(winners) == 1

    claimed_task = (
        race_store.get(
            race_task.id
        )
    )

    assert claimed_task is not None

    assert (
        claimed_task.owner
        in {
            "worker_a",
            "worker_b",
        }
    )

    assert (
        claimed_task.status
        == "in_progress"
    )

    assert (
        claimed_task.attempts
        == 1
    )

    # ========================================================
    # PART 3
    # Create real Git repository
    # ========================================================

    repo = create_git_repo(
        tmp_path
    )

    # Runtime 数据故意放在 repo 外面。
    #
    # 否则 tasks.json / mailbox.json
    # 会让主仓库变 dirty，
    # 后面 merge_branch 会拒绝合并。

    runtime_root = (
        tmp_path
        / "runtime"
    )

    runtime_root.mkdir()

    # ========================================================
    # PART 4
    # Persistent Task Graph
    # ========================================================

    task_store = TaskStore(
        runtime_root
        / "tasks.json"
    )

    task_a = (
        task_store.create(
            title=(
                "Implement backend"
            ),
            description=(
                "Implement backend feature"
            ),
        )
    )

    task_b = (
        task_store.create(
            title=(
                "Add tests"
            ),
            description=(
                "Add unit tests"
            ),
        )
    )

    task_c = (
        task_store.create(
            title=(
                "Integration validation"
            ),
            description=(
                "Validate backend "
                "and tests together"
            ),
            depends_on=[
                task_a.id,
                task_b.id,
            ],
        )
    )

    graph = TaskGraph(
        task_store
    )

    runnable_ids = {
        task.id
        for task
        in graph.runnable()
    }

    assert runnable_ids == {
        task_a.id,
        task_b.id,
    }

    initial_c = (
        task_store.get(
            task_c.id
        )
    )

    assert initial_c is not None

    # C 此时依赖 A/B，
    # 所以 blocked。
    assert graph.is_blocked(
        initial_c
    )

    # ========================================================
    # PART 5
    # Mailbox + Worktrees
    # ========================================================

    mailbox = MailboxStore(
        runtime_root
        / "mailbox.json"
    )

    worktree_manager = (
        WorktreeManager(
            repo_root=repo,
            worktree_root=(
                tmp_path
                / "worktrees"
            ),
        )
    )

    # ========================================================
    # PART 6
    # Worker Agent Factory
    # ========================================================

    # 第一批有两个 runnable tasks，
    # 两个 Worker 必须同时运行。
    first_batch_barrier = (
        Barrier(2)
    )

    created_workers = {}

    def worker_factory(
        workdir: Path,
        agent_id: str,
        worker_mailbox: MailboxStore,
    ):

        agent = FakeWorkerAgent(
            workdir=workdir,
            agent_id=agent_id,
            first_batch_barrier=(
                first_batch_barrier
            ),
        )

        created_workers[
            agent_id
        ] = agent

        return agent

    # ========================================================
    # PART 7
    # TeamManager
    # ========================================================

    team = TeamManager(
        task_store=task_store,
        mailbox=mailbox,
        worktrees=(
            worktree_manager
        ),
        agent_factory=(
            worker_factory
        ),
        lead_id="lead",
    )

    worker_a = (
        team.add_teammate(
            agent_id="worker_a",
            role=(
                "Backend engineer"
            ),
        )
    )

    worker_b = (
        team.add_teammate(
            agent_id="worker_b",
            role=(
                "Testing engineer"
            ),
        )
    )

    # 再次获取同一个 teammate，
    # 应该得到原来的长期 Worker，
    # 而不是创建一个新 Agent。
    worker_a_again = (
        team.add_teammate(
            agent_id="worker_a",
            role=(
                "Backend engineer"
            ),
        )
    )

    assert (
        worker_a_again
        is worker_a
    )

    # ========================================================
    # PART 8
    # Lead → Worker Mailbox
    # ========================================================

    send_result = team.send(
        recipient="worker_a",
        content=(
            "Please inspect the lead "
            "instruction before working."
        ),
    )

    assert send_result.startswith(
        "Sent "
    )

    # ========================================================
    # PART 9
    # First parallel batch
    # ========================================================

    first_batch_result = (
        team.run_ready()
    )

    # A/B 均应该完成。
    refreshed_a = (
        task_store.get(
            task_a.id
        )
    )

    refreshed_b = (
        task_store.get(
            task_b.id
        )
    )

    assert refreshed_a is not None
    assert refreshed_b is not None

    assert (
        refreshed_a.status
        == "completed"
    )

    assert (
        refreshed_b.status
        == "completed"
    )

    assert {
        refreshed_a.owner,
        refreshed_b.owner,
    } == {
        "worker_a",
        "worker_b",
    }

    assert (
        task_a.id
        in first_batch_result
    )

    assert (
        task_b.id
        in first_batch_result
    )

    # ========================================================
    # PART 10
    # Dependency unlock
    # ========================================================

    refreshed_c = (
        task_store.get(
            task_c.id
        )
    )

    assert refreshed_c is not None

    # A/B 完成以后，
    # C 自动从 blocked → runnable。
    assert not graph.is_blocked(
        refreshed_c
    )

    runnable_after_batch = {
        task.id
        for task
        in graph.runnable()
    }

    assert runnable_after_batch == {
        task_c.id
    }

    # ========================================================
    # PART 11
    # Verify Lead → Worker mailbox reached prompt
    # ========================================================

    worker_a_agent = (
        created_workers[
            "worker_a"
        ]
    )

    assert (
        "Please inspect the lead "
        "instruction before working."
        in worker_a_agent.prompts[0]
    )

    # ========================================================
    # PART 12
    # Persistent teammate
    # ========================================================

    assert (
        created_workers[
            "worker_a"
        ].calls
        == 1
    )

    assert (
        created_workers[
            "worker_b"
        ].calls
        == 1
    )

    # 第二批只有 Task C。
    #
    # TeamManager 会复用已有 teammate，
    # 不重新创建 Agent。

    second_batch_result = (
        team.run_ready()
    )

    final_c = (
        task_store.get(
            task_c.id
        )
    )

    assert final_c is not None

    assert (
        final_c.status
        == "completed"
    )

    assert (
        task_c.id
        in second_batch_result
    )

    # 当前实现 available teammate
    # 按注册顺序选择，
    # 所以 C 会再次给 worker_a。
    assert (
        created_workers[
            "worker_a"
        ].calls
        == 2
    )

    assert (
        created_workers[
            "worker_b"
        ].calls
        == 1
    )

    # 这证明 worker_a 是：
    #
    # Persistent Teammate
    #
    # 而不是每个 Task 都重新创建。

    # ========================================================
    # PART 13
    # Worker → Lead Mailbox
    # ========================================================

    lead_messages = mailbox.read(
        recipient="lead"
    )

    # A / B / C 每个任务结束，
    # teammate 都给 Lead 发消息。
    assert len(
        lead_messages
    ) == 3

    lead_text = "\n".join(
        message.content
        for message
        in lead_messages
    )

    assert task_a.id in lead_text
    assert task_b.id in lead_text
    assert task_c.id in lead_text

    # ========================================================
    # PART 14
    # Worktree isolation
    # ========================================================

    worker_a_file = (
        worker_a.worktree.path
        / "worker_a.txt"
    )

    worker_b_file = (
        worker_b.worktree.path
        / "worker_b.txt"
    )

    assert worker_a_file.exists()
    assert worker_b_file.exists()

    # 主 workspace 此时还不应该看到
    # Worker 的代码修改。
    assert not (
        repo / "worker_a.txt"
    ).exists()

    assert not (
        repo / "worker_b.txt"
    ).exists()

    # Worker A 做了两个任务，
    # 所以自己的 worktree 中有两次调用。
    assert (
        worker_a_file
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
        == [
            "worker_a call=1",
            "worker_a call=2",
        ]
    )

    assert (
        worker_b_file
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
        == [
            "worker_b call=1"
        ]
    )

    # ========================================================
    # PART 15
    # Verify Git commits
    # ========================================================

    worker_a_head = run_git(
        worker_a.worktree.path,
        "rev-parse",
        "HEAD",
    )

    worker_b_head = run_git(
        worker_b.worktree.path,
        "rev-parse",
        "HEAD",
    )

    main_head = run_git(
        repo,
        "rev-parse",
        "HEAD",
    )

    assert (
        worker_a_head
        != main_head
    )

    assert (
        worker_b_head
        != main_head
    )

    # ========================================================
    # PART 16
    # Merge worker branches
    # ========================================================

    worktree_manager.merge_branch(
        worker_a.worktree.branch
    )

    worktree_manager.merge_branch(
        worker_b.worktree.branch
    )

    # Merge 后修改正式进入主 workspace。

    assert (
        repo / "worker_a.txt"
    ).exists()

    assert (
        repo / "worker_b.txt"
    ).exists()

    assert (
        (
            repo / "worker_a.txt"
        )
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
        == [
            "worker_a call=1",
            "worker_a call=2",
        ]
    )

    assert (
        (
            repo / "worker_b.txt"
        )
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
        == [
            "worker_b call=1"
        ]
    )

    # ========================================================
    # PART 17
    # Final team state
    # ========================================================

    status = team.status()

    assert (
        "worker_a [idle]"
        in status
    )

    assert (
        "worker_b [idle]"
        in status
    )

    assert (
        f"{task_a.id} "
        "[completed]"
        in status
    )

    assert (
        f"{task_b.id} "
        "[completed]"
        in status
    )

    assert (
        f"{task_c.id} "
        "[completed]"
        in status
    )