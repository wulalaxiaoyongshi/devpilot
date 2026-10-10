from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


@dataclass
class WorktreeRef:
    agent_id: str
    path: Path
    branch: str


class WorktreeManager:

    def __init__(self,repo_root: Path,worktree_root: Path | None = None,):
        self.repo_root = (repo_root.resolve())
        self.worktree_root = (
            worktree_root
            or (
                self.repo_root.parent/ (self.repo_root.name+ "-worktrees")
            )
        ).resolve()
        self.worktree_root.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._ensure_git_repo()

    def create(self,agent_id: str,) -> WorktreeRef:
        safe_id = self._safe_name(agent_id)
        path = (self.worktree_root / safe_id)
        branch = (f"devpilot/{safe_id}")
        # 已存在 → 重用
        if path.exists():
            existing_branch = (
                self._run(
                    [
                        "git",
                        "branch",
                        "--show-current",
                    ],
                    cwd=path,
                )
                .stdout
                .strip()
            )
            return WorktreeRef(
                agent_id=agent_id,
                path=path,
                branch=(
                    existing_branch
                    or branch
                ),
            )

        branch_exists = (
            subprocess.run(
                [
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                ],
                cwd=self.repo_root,
            ).returncode
            == 0
        )

        if branch_exists:

            self._run(
                [
                    "git",
                    "worktree",
                    "add",
                    str(path),
                    branch,
                ],
                cwd=self.repo_root,
            )

        else:

            self._run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    "HEAD",
                ],
                cwd=self.repo_root,
            )

        return WorktreeRef(
            agent_id=agent_id,
            path=path,
            branch=branch,
        )

    def commit_all(self,worktree: WorktreeRef,message: str) -> str | None:

        status = (
            self._run(
                [
                    "git",
                    "status",
                    "--porcelain",
                ],
                cwd=worktree.path,
            )
            .stdout
            .strip()
        )

        if not status:
            return None

        self._run(
            [
                "git",
                "add",
                "-A",
            ],
            cwd=worktree.path,
        )

        self._run(
            [
                "git",
                "commit",
                "-m",
                message,
            ],
            cwd=worktree.path,
        )

        commit = (
            self._run(
                [
                    "git",
                    "rev-parse",
                    "HEAD",
                ],
                cwd=worktree.path,
            )
            .stdout
            .strip()
        )

        return commit

    def merge_branch(self, branch: str) -> None:

        status = (
            self._run(
                [
                    "git",
                    "status",
                    "--porcelain",
                ],
                cwd=self.repo_root,
            )
            .stdout
            .strip()
        )

        if status:

            raise RuntimeError(
                "Main workspace is not clean"
            )

        self._run(
            [
                "git",
                "merge",
                "--no-ff",
                branch,
                "-m",
                (
                    "Merge DevPilot "
                    f"worker branch {branch}"
                ),
            ],
            cwd=self.repo_root,
        )

    def _ensure_git_repo(self) -> None:
        result = subprocess.run(
            [
                "git",
                "rev-parse",
                "--show-toplevel",
            ],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"{self.repo_root} "
                "is not a Git repository"
            )

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(
            r"[^A-Za-z0-9_.-]",
            "_",
            value,
        )

    @staticmethod
    def _run(
        command: list[str],
        cwd: Path,
    ) -> subprocess.CompletedProcess:

        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:

            raise RuntimeError(
                f"Command failed:\n"
                f"{' '.join(command)}\n\n"
                f"{result.stderr}"
            )

        return result