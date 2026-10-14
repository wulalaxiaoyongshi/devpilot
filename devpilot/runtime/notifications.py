"""Lead 消息存储：按事件 ID 幂等写入，崩溃后可重新加载。"""  # 提供独立于短期 Agent Context 的持久化通知

import json  # 序列化通知文件
from dataclasses import asdict, dataclass  # 定义通知的数据结构
from pathlib import Path  # 处理通知文件路径
from threading import RLock  # 保护线程内读写


@dataclass(frozen=True)  # 通知生成后不允许直接修改
class LeadNotice:  # 表示已经处理的单个后台事件
    event_id: str  # 使用原事件 ID 做去重键
    job_id: str  # 对应后台 Job
    event_type: str  # job.completed / job.failed 等
    task_id: str | None  # 对应 V3 持久 Task，可为空
    summary: str  # 给 Lead 的人类可读摘要


class LeadNoticeStore:  # 管理已收到的 Lead 事件通知
    def __init__(self, path: Path):  # 指定磁盘通知文件
        self.path = path.resolve()  # 转换为绝对路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
        self._lock = RLock()  # 保护当前 Store 实例
        if not self.path.exists():  # 仅第一次运行时初始化
            self._write([])  # 存入空列表

    def all(self) -> list[LeadNotice]:  # 返回完整历史通知列表
        with self._lock:  # 保护读操作
            return self._read()  # 解码文件中的通知

    def add_once(self, notice: LeadNotice) -> bool:  # 按 event_id 保证一次通知入库
        with self._lock:  # 检查和保存必须在同一临界区
            items = self._read()  # 取出当前通知
            if any(item.event_id == notice.event_id for item in items):  # 防止重复事件投递
                return False  # 此事件已经记录
            items.append(notice)  # 添加本次通知
            self._write(items)  # 同步保存到磁盘
            return True  # 表示新通知已经保存

    def _read(self) -> list[LeadNotice]:  # 内部读函数，要求外层加锁
        data = json.loads(self.path.read_text(encoding="utf-8"))  # 解码 JSON
        if not isinstance(data, list):  # 防止意外格式
            raise ValueError("Lead notices must be a JSON array")  # 报告损坏文件
        return [LeadNotice(**record) for record in data]  # 转换成不可变快照

    def _write(self, items: list[LeadNotice]) -> None:  # 原子替换通知 JSON 文件
        temp = self.path.with_name(self.path.name + ".tmp")  # 临时文件需位于同一目录
        temp.write_text(json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2), encoding="utf-8")  # 序列化通知
        temp.replace(self.path)  # 一次替换正式文件
