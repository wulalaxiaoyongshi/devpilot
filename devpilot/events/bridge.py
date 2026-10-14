from collections.abc import Callable  # 标注完成回调的函数类型

from devpilot.background.model import BackgroundJob  # V5.1 的后台任务快照
from devpilot.background.store import JobStore  # 用于漏发事件补偿
from devpilot.events.bus import EventBus  # 事件队列与日志
from devpilot.events.model import JOB_EVENT_TYPES, RuntimeEvent  # 终态集合与事件转换


def make_job_event_callback(bus: EventBus) -> Callable[[BackgroundJob], None]:  # 生成适配 V5.1 on_finished 的回调
    def on_finished(job: BackgroundJob) -> None:  # V5.1 完成任务时会调用这个函数
        bus.publish(RuntimeEvent.from_job(job))  # 把终态 Job 转成事件并入队
    return on_finished  # 返回函数对象，而不是执行函数


def reconcile_terminal_jobs(store: JobStore, bus: EventBus) -> int:  # 弥补完成回调曾经失败或进程重启导致的事件缺口
    published = 0  # 统计本次补发的事件数
    for job in store.all():  # 扫描持久化的 Job 状态
        if job.status not in JOB_EVENT_TYPES:  # 忽略仍在运行的任务
            continue  # 等该任务真正完成再发布
        if bus.publish(RuntimeEvent.from_job(job)):  # 使用确定性 Event ID 去重
            published += 1  # 只有新发布的事件才计入
    return published  # 返回新补发的事件数量
