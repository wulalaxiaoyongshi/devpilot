import json  # JSON Lines 编码与解码
from dataclasses import asdict  # 将 RuntimeEvent 转成 JSON 字典
from pathlib import Path  # 处理事件日志路径
from threading import RLock  # 保证同一个 Journal 对象的线程安全

from devpilot.events.model import RuntimeEvent  # 统一事件数据结构


class EventJournal:  # 将已发布事件和已处理确认追加保存到磁盘
    def __init__(self, path: Path):  # 接收事件日志文件的路径
        self.path = path.resolve()  # 统一绝对路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 创建存储目录
        self._lock = RLock()  # 防止多个 Worker 交错写日志
        self._events: dict[str, RuntimeEvent] = {}  # 记录已发布事件，按插入顺序排列
        self._acked: set[str] = set()  # 记录已成功分发的事件 ID
        self.path.touch(exist_ok=True)  # 不存在时创建空 JSONL 文件
        self._load()  # 从磁盘恢复旧事件和确认状态

    def _load(self) -> None:  # 启动时逐条回放日志，重建内存索引
        with self._lock:  # 恢复时不允许其他线程操作同一 Journal
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):  # 逐行读取 JSONL
                if not line.strip():  # 跳过空行
                    continue  # 继续下一行
                try:  # 明确报告损坏的日志，而不是静默跳过
                    record = json.loads(line)  # 解析 JSON 字符串
                    kind = record["kind"]  # 判断是发布还是确认记录
                    if kind == "published":  # 事件发布记录
                        event = RuntimeEvent(**record["event"])  # 恢复事件对象
                        if event.id in self._events:  # 一个 ID 不应有两次发布
                            raise ValueError(f"Duplicate published event: {event.id}")  # 报告不一致
                        self._events[event.id] = event  # 保存事件索引
                    elif kind == "acked":  # 事件被成功处理的确认记录
                        event_id = record["event_id"]  # 获取被确认的事件 ID
                        if event_id not in self._events:  # 确认必须对应一个已发布事件
                            raise ValueError(f"Ack references unknown event: {event_id}")  # 拒绝损坏记录
                        self._acked.add(event_id)  # 标记已处理
                    else:  # 不认识的日志记录类型
                        raise ValueError(f"Unknown journal record kind: {kind}")  # 拒绝不兼容格式
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:  # 统一提供定位信息
                    raise ValueError(f"Invalid event journal line {line_number}: {error}") from error  # 暴露有问题的行号

    def _append_record(self, record: dict) -> None:  # 向 JSONL 文件追加一条记录
        text = json.dumps(record, ensure_ascii=False)  # 把记录变成单行 JSON
        with self.path.open("a", encoding="utf-8") as handle:  # 以追加模式打开
            handle.write(text + "\n")  # 每条记录占一行
            handle.flush()  # 发送缓冲内容给操作系统；不是跨进程事务保证

    def publish(self, event: RuntimeEvent) -> bool:  # 持久化事件；已经存在则返回 False
        with self._lock:  # 查重与追加在同一个临界区
            if event.id in self._events:  # 确定性 ID 保证同一个 Job 只发布一次
                return False  # 重复事件不再次入队
            self._append_record({"kind": "published", "event": asdict(event)})  # 先写磁盘再允许入队
            self._events[event.id] = event  # 日志写成功才更新内存索引
            return True  # 表示这是首次发布

    def ack(self, event_id: str) -> bool:  # 标记事件已经被 Dispatcher 成功处理
        with self._lock:  # 确认状态与文件追加保持一致
            if event_id not in self._events:  # 不允许确认未发布的事件
                raise KeyError(f"Unknown event: {event_id}")  # 帮助排查调用方错误
            if event_id in self._acked:  # 重复确认不应额外写日志
                return False  # 已确认过
            self._append_record({"kind": "acked", "event_id": event_id})  # 持久化处理完成的事实
            self._acked.add(event_id)  # 更新本进程内的已处理集合
            return True  # 第一次确认成功

    def pending(self) -> list[RuntimeEvent]:  # 查询所有已发布但未成功确认的事件
        with self._lock:  # 获取一致的快照
            return [event for event_id, event in self._events.items() if event_id not in self._acked]  # 保留原发布顺序

    def has_event(self, event_id: str) -> bool:  # 检查是否已经持久化这个事件
        with self._lock:  # 与写入方同步
            return event_id in self._events  # 返回布尔值
