from dataclasses import dataclass, field  # 数据类与默认值工厂
from datetime import datetime, timezone  # 统一使用 UTC 时间
from threading import Event  # 线程之间传递取消信号
from typing import Literal  # 限定状态可选值


def utc_now() -> str:  # 生成便于保存到 JSON 的时间字符串
    return datetime.now(timezone.utc).isoformat()  # 使用带时区的 UTC 时间


JobStatus = Literal["pending", "running", "cancelling", "completed", "failed", "cancelled", "interrupted"]  # 状态集合


@dataclass(frozen=True)  # Job 是不可原地修改的状态快照
class BackgroundJob:  # 表示一次后台执行
    id: str  # 唯一任务 ID
    name: str  # 人类可读的任务名称
    status: JobStatus = "pending"  # 初始为等待执行
    task_id: str | None = None  # 可关联 V3 的持久化 Task
    result: str | None = None  # 成功时的文本结果
    error: str | None = None  # 失败或中断原因
    created_at: str = field(default_factory=utc_now)  # 创建时间
    started_at: str | None = None  # 实际启动时间
    finished_at: str | None = None  # 最终结束时间


class JobCancelled(Exception):  # 后台任务主动响应取消时抛出的异常
    pass  # 不需要自定义其他行为


class JobContext:  # 传递给实际后台函数的运行上下文
    def __init__(self, job_id: str, cancel_event: Event):  # 接收 ID 和取消信号
        self.job_id = job_id  # 保存任务 ID
        self._cancel_event = cancel_event  # 不向外暴露可任意修改的 Event

    @property  # 允许通过属性方式读取
    def cancel_requested(self) -> bool:  # 当前是否收到取消请求
        return self._cancel_event.is_set()  # Event 为 set 时返回 True

    def wait_for_cancellation(self, timeout: float | None = None) -> bool:  # 等待取消信号，便于无轮询测试
        return self._cancel_event.wait(timeout=timeout)  # 收到取消返回 True，超时返回 False

    def raise_if_cancelled(self) -> None:  # 给后台任务显式检查取消的入口
        if self.cancel_requested:  # 只有收到取消信号才中断
            raise JobCancelled(f"Job {self.job_id} cancelled")  # 交给 Manager 统一记为 cancelled
