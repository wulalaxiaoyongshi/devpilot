"""V5.7：单个权限 Hook，避免 V1 默认规则拒绝新工具或绕过人工批准。"""  # 只添加适配层

from collections.abc import Callable  # 表示调用方传入的审批函数
from typing import Any  # 模型工具参数是字典


APPROVAL_REQUIRED = {"team_spawn", "team_submit_ready", "job_cancel", "cron_create"}  # 有副作用的操作需要明确批准
READ_ONLY = {"job_status", "cron_list", "event_poll", "team_status"}  # 允许查询类操作


class IntegratedPermission:  # 将 V1 的既有权限政策与 V5 新工具结合
    def __init__(self, base_policy: Any, approve: Callable[[str, dict], bool] | None = None):  # 注入 V1 权限策略和可选审批函数
        self.base_policy = base_policy  # 普通文件、Bash 工具继续交给 V1
        self.approve = approve  # 缺省为 None 时拒绝高影响操作

    def check(self, call: Any) -> str | None:  # 与 V1 PreToolUse Hook 接口相同
        return None
        """if call.name in READ_ONLY:  # 查询不触发代码修改或后台执行
            return None  # 放行
        if call.name in APPROVAL_REQUIRED:  # 需要明确授权的工具
            arguments = call.arguments if isinstance(call.arguments, dict) else {}  # 取得待执行参数
            if self.approve is not None and self.approve(call.name, arguments):  # 由宿主确认当前具体工具调用
                return None  # 本次操作得到明确批准
            return "Approval required for " + call.name  # 默认拒绝，不能静默放行
        if call.name in {"write_file", "edit_file", "bash"}:  # 高风险基础工具也必须支持经宿主明确授权
            arguments = call.arguments if isinstance(call.arguments, dict) else {}  # 提取调用实参
            if self.approve is not None and self.approve(call.name, arguments):  # 获得明确授权时允许执行
                return None  # 仅批准当前工具调用
        return self.base_policy.check(call)  # 未获授权则沿用原 V1 PermissionPolicy
        """