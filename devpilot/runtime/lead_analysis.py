"""V9.2：复用真实 V5.7 Factory，将 Lead 限制为安全仓库分析和任务规划。"""

from pathlib import Path
from typing import Any, Callable

from devpilot.runtime.factory import create_app
from devpilot.tasks.model import Task


class LeadAnalysisApp:
    """负责分析阶段的入口、任务快照和真实 V5 应用生命周期。"""

    def __init__(self, core: Any, repo_root: Path):
        """保存已组装的真实 Agent 应用和待分析仓库位置。"""
        self.core = core
        self.repo_root = repo_root.resolve()

    def analyze(self, request: str) -> str:
        """把用户需求交给真实 Lead Agent Loop，而不是调用预设的修复函数。"""
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must be a nonempty string")
        return self.core.chat(request)

    def tasks(self) -> list[Task]:
        """读取 Lead 在 V3 TaskStore 中实际创建的持久化任务。"""
        return self.core.lead.tasks.store.all()

    def tools(self) -> tuple[str, ...]:
        """返回真实发送给模型的分析工具白名单。"""
        return tuple(self.core.lead.tools.names())

    def close(self) -> None:
        """调用已有 DevPilotApp.shutdown 回收后台线程和事件资源。"""
        self.core.shutdown()

    def __enter__(self) -> "LeadAnalysisApp":
        """允许用 with 自动管理分析会话的生命周期。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """退出分析会话时无论是否异常都关闭真实应用。"""
        self.close()
        return False


def create_lead_analysis_app(repo_root: Path, model_factory: Callable[[str], Any], *, state_dir: Path | None = None) -> LeadAnalysisApp:
    """复用 V5.7 的真实依赖注入，仅为 Lead 启用最小权限分析模式。"""
    core = create_app(repo_root=repo_root, model_factory=model_factory, approve=lambda _tool, _args: False,
                      state_dir=state_dir, auto_extract=False, lead_mode="analysis")
    return LeadAnalysisApp(core, repo_root)
