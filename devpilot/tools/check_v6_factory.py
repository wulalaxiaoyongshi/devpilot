"""V6.6：在真正连接外部 Server 之前检查用户工程的 V5.7/V6.2 权限接线。"""

import inspect


def main() -> None:
    """运行时检查 V5.7 Factory 是否通过 MCPPermissionPolicy 组装 V1 的权限链，失败便退出。"""
    from devpilot.runtime import factory

    source = inspect.getsource(factory.create_app)
    if "MCPPermissionPolicy" not in source or "register_pre_tool_use(policy.check)" not in source:
        raise SystemExit("MCP permission factory wiring was not detected; review V6.2 patch before starting V6.6")
    print("V6.2 factory permission wiring detected; run end-to-end permission tests next.")


if __name__ == "__main__":
    main()
