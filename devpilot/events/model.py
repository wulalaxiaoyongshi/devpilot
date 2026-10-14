from dataclasses import dataclass, field  # 使用数据类描述不可变事件
from typing import Literal  # 约束事件类型的合法值

from devpilot.background.model import BackgroundJob, utc_now  # 复用 V5.1 的任务快照和 UTC 时间函数


EventType = Literal["job.completed", "job.failed", "job.cancelled", "job.interrupted"]  # V5.2 处理的四种终态事件
JOB_EVENT_TYPES = {"completed", "failed", "cancelled", "interrupted"}  # 只有终态才能发布完成事件
EVENT_TYPES: dict[str, EventType] = {"completed": "job.completed", "failed": "job.failed", "cancelled": "job.cancelled", "interrupted": "job.interrupted"}  # 用确定性映射生成合法事件类型


@dataclass(frozen=True)  # Event 创建后不允许被处理器原地篡改
class RuntimeEvent:  # 一条可持久化、可分发的事件
    id: str  # 事件唯一 ID，用于去重和确认
    type: EventType  # 用于选择事件处理器
    job_id: str  # 对应 V5.1 的后台执行 ID
    task_id: str | None = None  # 可选，对应 V3 的持久任务 ID
    created_at: str = field(default_factory=utc_now)  # 事件创建时间

    @classmethod  # 提供从后台任务快照创建事件的构造入口
    def from_job(cls, job: BackgroundJob) -> "RuntimeEvent":  # 输入：一个已经结束的 BackgroundJob
        if job.status not in JOB_EVENT_TYPES:  # 非终态不能冒充完成事件
            raise ValueError(f"Job is not in a terminal state: {job.status}")  # 阻止提前发布
        return cls(id=f"event_{job.id}", type=EVENT_TYPES[job.status], job_id=job.id, task_id=job.task_id)  # 使用确定性 ID 防重复
