"""V9.3：复用原有工厂提供真实 Lead、Worker、后台任务和事件通知的统一入口。"""

from pathlib import Path
from typing import Any, Callable

from devpilot.runtime.factory import create_app
from devpilot.tasks.model import Task


class LeadTeamApp:
    """管理 V0～V5 的真实团队会话，并向在线验收程序暴露可查询的执行事实。"""

    def __init__(self, core: Any, repo_root: Path):
        """保存工厂创建的真实应用和所操作的 Git 仓库路径。"""
        self.core = core
        self.repo_root = repo_root.resolve()

    def instruct(self, text: str) -> str:
        """将用户指令交给真实 Lead Agent Loop，并由注册的工具决定后续动作。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("instruction must be a nonempty string")
        return self.core.chat(text)

    def tasks(self) -> list[Task]:
        """直接从 V3 持久化 TaskStore 查询所有团队任务。"""
        return self.core.team.task_store.all()

    def jobs(self) -> list[Any]:
        """直接从 V5 持久化 JobStore 查询当前所有后台任务。"""
        return self.core.jobs.store.all()

    def teammates(self) -> dict[str, Any]:
        """返回 V4 TeamManager 已创建的 Worker 快照。"""
        return dict(self.core.team.teammates)

    def pump(self, *, resume_lead: bool = True) -> Any:
        """先把完成事件写入 LeadNoticeStore，再按需调用真实 Lead 消费通知。"""
        return self.core.pump(resume_lead=resume_lead)

    def pending_notices(self) -> list[Any]:
        """读取已持久化但尚未投递给 Lead 的后台完成通知。"""
        return self.core.delivery.pending()

    def close(self) -> None:
        """按 V5 已有的关闭顺序等待后台任务并回收资源。"""
        self.core.shutdown()

    def __enter__(self) -> "LeadTeamApp":
        """支持 with 自动管理团队会话，保证测试异常时也释放线程池。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """离开团队会话时关闭后台资源，同时保留原异常供调用者处理。"""
        self.close()
        return False


def create_lead_team_app(repo_root: Path, model_factory: Callable[[str], Any], *, state_dir: Path | None = None,
                         worker_write_files: tuple[str, ...] = (), allow_team_dispatch: bool = False,
                         max_workers: int = 2, allowed_worker_ids: tuple[str, ...] = ("backend", "tests"),
                         allowed_lead_mcp_tools: frozenset[str] = frozenset(), mcp_authorizer: Any | None = None,
                         worker_write_map: dict[str, tuple[str, ...]] | None = None) -> LeadTeamApp:
    """通过 V5.7 的真实 create_app 接通 V0～V5，并要求宿主明确授权团队派发和可写文件。"""
    if not worker_write_files:
        raise ValueError("worker_write_files must be explicitly configured")
    core = create_app(repo_root, model_factory, approve=lambda _name, _arguments: False,
                      state_dir=state_dir, max_workers=max_workers, auto_extract=False,
                      lead_mode="team", worker_write_files=worker_write_files,
                      allow_team_dispatch=allow_team_dispatch, allowed_worker_ids=allowed_worker_ids,
                      allowed_lead_mcp_tools=allowed_lead_mcp_tools, mcp_authorizer=mcp_authorizer, worker_write_map=worker_write_map)
    return LeadTeamApp(core, repo_root)
