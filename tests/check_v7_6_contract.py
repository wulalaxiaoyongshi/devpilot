"""运行前检查 V7.6 自身的接口是否齐全，并报告旧版 Runtime 模块是否可导入。"""

from __future__ import annotations

import importlib
import sys
from importlib.util import find_spec
from pathlib import Path


def main() -> int:
    """验证 V7.6 本地公开接口并提示哪些跨版本模块仍需要真实项目验收。"""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    workflow = importlib.import_module("devpilot.workflow")
    required = (
        "WorkflowApplication", "WorkflowBindings", "ApprovalInboxItem",
        "build_coding_workflow", "register_coding_verification",
        "WorkflowEngine", "ParallelWorkflowRunner", "WorkflowRuntimeAdapters",
    )
    missing = [name for name in required if not hasattr(workflow, name)]
    print("[V7.6 Workflow 接口]", "PASS" if not missing else f"MISSING {missing}")
    modules = (
        "devpilot.core.agent", "devpilot.tools.dispatcher", "devpilot.tasks.store",
        "devpilot.agents.team", "devpilot.jobs.manager", "devpilot.mcp",
    )
    for module in modules:
        try:
            status = "可导入" if find_spec(module) is not None else "尚未接入"
        except (ModuleNotFoundError, AttributeError):
            status = "尚未接入"
        print(f"[旧版模块] {module}: {status}")
    print("[注意] 仅检查模块位置，不证明旧接口的调用参数或真实集成通过。")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
