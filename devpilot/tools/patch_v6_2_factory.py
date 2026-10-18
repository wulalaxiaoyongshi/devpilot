"""V6.2：在保留 V5.7 权限机制的前提下，为 MCP 工具接入专门的审批策略。"""

import sys
from pathlib import Path


def main() -> None:
    """用户在项目根目录运行时，仅修改 V5.7 Factory 的一条权限构造语句与 import。"""
    project = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    target = project / "devpilot" / "runtime" / "factory.py"
    content = target.read_text(encoding="utf-8")
    old = "policy = IntegratedPermission(PermissionPolicy(), approve=approve)"
    new = "policy = IntegratedPermission(MCPPermissionPolicy(PermissionPolicy(), approve=approve), approve=approve)"
    imported = "from devpilot.mcp.permission import MCPPermissionPolicy\n"
    if new in content and imported in content:
        print("V6.2 Factory patch already applied")
        return
    if content.count(old) != 1 or "from devpilot.runtime.permission import IntegratedPermission" not in content:
        raise RuntimeError("Factory layout differs from V5.7; inspect manually instead of overwriting")
    content = content.replace(old, new, 1)
    content = content.replace("from devpilot.runtime.permission import IntegratedPermission", imported + "from devpilot.runtime.permission import IntegratedPermission", 1)
    target.write_text(content, encoding="utf-8")
    print("V6.2 MCP permission integration patch applied")


if __name__ == "__main__":
    main()
