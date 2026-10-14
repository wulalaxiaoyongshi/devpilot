"""V5.4 的持久化定时计划数据结构。"""  # 定时计划不是 V5.1 的后台执行实例

from dataclasses import dataclass, field  # 用数据类记录计划元数据
from typing import Any, Literal  # 为 JSON 参数和漏执行策略标注类型

from devpilot.background.model import utc_now  # 复用项目中的 UTC 时间格式


MisfirePolicy = Literal["skip", "fire_once"]  # skip 表示略过过时触发；fire_once 表示补执行一次


@dataclass(frozen=True)  # 每次操作创建新的不可变计划快照
class CronSchedule:  # 一条可以跨进程重启保存的定时计划
    id: str  # 定时计划 ID，同一个计划可产生多个 Job
    name: str  # 给用户看的计划名称
    expression: str  # 五字段 Cron 原始表达式
    action: str  # 可重建的动作名称，不能持久化 Python 函数对象
    payload: dict[str, Any]  # 通过 JSON 序列化保存的动作参数
    timezone: str = "UTC"  # IANA 时区，例如 Asia/Shanghai
    misfire_policy: MisfirePolicy = "skip"  # 错过执行时的策略
    enabled: bool = True  # 是否允许产生新 Job
    next_run_at: str | None = None  # 下一次触发的 UTC ISO 时间
    last_slot_at: str | None = None  # 最后一次处理过的 Cron 时间槽
    last_job_id: str | None = None  # 最近一次成功提交的后台 Job ID
    last_error: str | None = None  # 最近一次提交失败原因
    created_at: str = field(default_factory=utc_now)  # 计划创建时间
