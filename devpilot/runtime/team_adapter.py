"""V5.7：为旧版 V4 私有任务执行入口提供向后兼容的公开调用适配。"""  # 不破坏 V4 原来全部行为

from typing import Any  # 允许用户的具体 TeamManager 子类


def invoke_team_assignment(team: Any, task_id: str, agent_id: str) -> str:  # 统一调用一项已经选定的任务分配
    public = getattr(team, "run_assignment", None)  # 优先查找未来 V4 暴露的公开接口
    if callable(public):  # 新 V4 已经支持公开方法
        return public(task_id, agent_id)  # 使用正式公开方法
    legacy = getattr(team, "_run_assignment", None)  # 对旧 V4 保留向后兼容
    if callable(legacy):  # 当前 V4 的真实接口仍是私有方法
        return legacy(task_id, agent_id)  # 不复制 Ownership / Commit 执行逻辑
    raise TypeError("TeamManager must expose run_assignment or _run_assignment")  # 接口不匹配时明确失败
