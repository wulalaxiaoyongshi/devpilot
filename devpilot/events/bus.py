from queue import Empty, Queue  # 使用线程安全 FIFO 队列跨线程传递事件

from devpilot.events.journal import EventJournal  # 追加式事件持久化层
from devpilot.events.model import RuntimeEvent  # 事件数据结构


class EventBus:  # 负责事件发布、暂存、提取与处理确认
    def __init__(self, journal: EventJournal):  # 每个 Bus 持有一个日志
        self.journal = journal  # 保存日志实例
        self._queue: Queue[RuntimeEvent] = Queue()  # 线程安全的内存队列
        for event in self.journal.pending():  # 启动时加载上次尚未确认的事件
            self._queue.put(event)  # 重新排队等待 Lead 处理

    def publish(self, event: RuntimeEvent) -> bool:  # Worker 发布一个事件
        if not self.journal.publish(event):  # 先持久化；日志会判断重复事件
            return False  # 已存在则不重复入队
        self._queue.put(event)  # 真正放入线程安全队列供 Lead 消费
        return True  # 发布成功

    def poll_nowait(self) -> RuntimeEvent | None:  # Lead 立即取一条事件，不阻塞
        try:  # 空队列是正常情况
            return self._queue.get_nowait()  # 有事件时立即返回
        except Empty:  # 当前没有待处理事件
            return None  # 返回 None 表示无事件

    def poll(self, timeout: float) -> RuntimeEvent | None:  # 等待一段时间获取事件
        if timeout < 0:  # 防止负超时参数
            raise ValueError("timeout must be >= 0")  # 提前报错
        try:  # 等待事件到达或超时
            return self._queue.get(timeout=timeout)  # 取到事件后返回
        except Empty:  # 指定时间没有新事件
            return None  # 没有新事件

    def requeue(self, event: RuntimeEvent) -> None:  # 分发失败后重新加入队列
        self._queue.put(event)  # 不再写 Journal，因为事件已经存在

    def ack(self, event: RuntimeEvent) -> None:  # 处理器成功执行后确认事件
        self.journal.ack(event.id)  # 记录已经处理完成；重启后不再重放

    def pending_in_memory(self) -> int:  # 查询目前待处理的内存队列长度
        return self._queue.qsize()  # 返回近似数量，不能作为严格并发同步条件
