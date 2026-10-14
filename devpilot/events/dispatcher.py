from collections.abc import Callable  # 事件回调函数类型

from devpilot.events.bus import EventBus  # 事件队列与确认入口
from devpilot.events.model import EventType, RuntimeEvent  # 类型定义和具体事件


EventHandler = Callable[[RuntimeEvent], None]  # 处理器接收一个事件，没有返回值


class EventDispatcher:  # 在 Lead 线程中把事件交给相应处理器
    def __init__(self, bus: EventBus):  # 注入事件总线
        self.bus = bus  # 保存事件来源
        self._handlers: dict[str, list[EventHandler]] = {}  # 每个事件类型对应多个处理器

    def register(self, event_type: EventType, handler: EventHandler) -> None:  # 注册某类事件的处理器
        if not callable(handler):  # 校验传进来的是函数或可调用对象
            raise TypeError("handler must be callable")  # 报告非法配置
        self._handlers.setdefault(event_type, []).append(handler)  # 按注册顺序保存处理器

    def process_available(self, max_events: int = 100) -> int:  # Lead 非阻塞处理当前队列中的事件
        if max_events < 1:  # 防止 0 或负数
            raise ValueError("max_events must be >= 1")  # 参数错误
        processed = 0  # 记录成功处理的事件数
        for _ in range(max_events):  # 每次调用最多处理固定数量，避免长时间阻塞 Lead
            event = self.bus.poll_nowait()  # 不等待新事件，只拿当前可用的
            if event is None:  # 内存队列已经空了
                break  # 退出分发循环
            try:  # 处理器失败时事件不能确认
                for handler in self._handlers.get(event.type, []):  # 逐一通知订阅该类型的处理器
                    handler(event)  # 真正执行事件处理逻辑
                self.bus.ack(event)  # 全部处理器成功后才确认
            except Exception:  # 任何处理器或确认过程失败
                self.bus.requeue(event)  # 将未确认的事件重新入队
                raise  # 让 Lead 知道处理失败并决定如何重试
            processed += 1  # 记录成功分发一条事件
        return processed  # 返回本次成功确认的事件数
