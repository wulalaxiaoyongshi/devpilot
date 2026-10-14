"""JSON 定时计划存储：同一 Python 进程共享一个 Store 实例。"""  # 不保证跨进程锁

import json  # 持久化计划
from dataclasses import asdict, replace  # 序列化数据类并生成更新快照
from pathlib import Path  # 管理计划文件路径
from threading import RLock  # 避免同一 Store 实例被多个线程同时改写

from devpilot.scheduler.model import CronSchedule  # 定时计划数据结构


class CronStore:  # 计划持久化、原子消费时间槽
    def __init__(self, path: Path):  # 接收 JSON 存储文件路径
        self.path = path.resolve()  # 将路径规范化
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 确保父目录存在
        self._lock = RLock()  # 同一实例内统一使用这把锁
        with self._lock:  # 初始化也需要互斥
            if not self.path.exists():  # 已有数据不能覆盖
                self._write_unlocked([])  # 首次创建空列表

    def all(self) -> list[CronSchedule]:  # 列出持久化计划
        with self._lock:  # 防止同时写入
            return self._read_unlocked()  # 返回解码后的数据对象

    def get(self, schedule_id: str) -> CronSchedule | None:  # 按 ID 查计划
        with self._lock:  # 读写使用相同锁
            return next((item for item in self._read_unlocked() if item.id == schedule_id), None)  # 不存在时返回 None

    def add(self, item: CronSchedule) -> None:  # 插入新计划
        with self._lock:  # 把读取、去重、写入放在同一个临界区
            schedules = self._read_unlocked()  # 取得最新列表
            if any(existing.id == item.id for existing in schedules):  # 检查 ID 冲突
                raise ValueError(f"Duplicate cron schedule: {item.id}")  # 不允许覆盖旧计划
            schedules.append(item)  # 追加一项
            self._write_unlocked(schedules)  # 持久化

    def update(self, schedule_id: str, **changes) -> CronSchedule:  # 原子更新单条计划
        with self._lock:  # 持续占用锁直至保存完成
            schedules = self._read_unlocked()  # 读取最新列表
            for index, item in enumerate(schedules):  # 查找目标
                if item.id == schedule_id:  # 找到计划
                    updated = replace(item, **changes)  # 创建修改后的快照
                    schedules[index] = updated  # 替换旧快照
                    self._write_unlocked(schedules)  # 保存更新结果
                    return updated  # 返回最新快照
            raise KeyError(f"Unknown cron schedule: {schedule_id}")  # 未找到时明确报错

    def remove(self, schedule_id: str) -> bool:  # 删除计划，不会取消已经启动的后台 Job
        with self._lock:  # 整个删除操作都持锁
            schedules = self._read_unlocked()  # 读取全部计划
            remaining = [item for item in schedules if item.id != schedule_id]  # 移除目标 ID
            if len(remaining) == len(schedules):  # 没有匹配记录
                return False  # 返回未删除
            self._write_unlocked(remaining)  # 保存剩余计划
            return True  # 返回成功

    def claim_slot(self, schedule_id: str, expected_slot: str, next_run_at: str) -> bool:  # 原子认领一次到期触发
        with self._lock:  # 防止多个 tick 重复认领同一个槽位
            schedules = self._read_unlocked()  # 读取磁盘状态
            for index, item in enumerate(schedules):  # 查找计划
                if item.id != schedule_id:  # 非目标跳过
                    continue  # 检查下一项
                if not item.enabled or item.next_run_at != expected_slot:  # 状态变化后不允许认领旧时间槽
                    return False  # 其他线程已经处理过或计划已暂停
                schedules[index] = replace(item, next_run_at=next_run_at, last_slot_at=expected_slot, last_job_id=None, last_error=None)  # 先推进下一次时间，避免重复提交
                self._write_unlocked(schedules)  # 把认领动作持久化
                return True  # 认领成功
            return False  # 计划被删除

    def record_submission(self, schedule_id: str, expected_slot: str, job_id: str | None = None, error: str | None = None) -> None:  # 记录一次认领的提交结果
        with self._lock:  # 与其他状态更新互斥
            schedules = self._read_unlocked()  # 获取最新状态
            for index, item in enumerate(schedules):  # 找到需要更新的计划
                if item.id == schedule_id and item.last_slot_at == expected_slot:  # 防止迟到的旧提交覆盖新槽位
                    schedules[index] = replace(item, last_job_id=job_id, last_error=error)  # 写入 Job ID 或错误说明
                    self._write_unlocked(schedules)  # 持久化结果
                    return  # 完成记录

    def _read_unlocked(self) -> list[CronSchedule]:  # 内部调用时必须已经持锁
        data = json.loads(self.path.read_text(encoding="utf-8"))  # 解析 JSON 数据
        if not isinstance(data, list):  # 文件格式不符合预期
            raise ValueError("Cron store must contain a JSON array")  # 拒绝不合法内容
        return [CronSchedule(**record) for record in data]  # 转换为 CronSchedule 数据类

    def _write_unlocked(self, schedules: list[CronSchedule]) -> None:  # 内部原子替换存储文件
        temporary = self.path.with_name(self.path.name + ".tmp")  # 在相同目录写临时文件
        temporary.write_text(json.dumps([asdict(item) for item in schedules], ensure_ascii=False, indent=2), encoding="utf-8")  # 先完成 JSON 序列化和写入
        temporary.replace(self.path)  # 替换正式文件，避免常规写入造成半截 JSON
