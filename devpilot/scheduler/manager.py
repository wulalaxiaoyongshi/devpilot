"""V5.4 定时调度器：可恢复计划、模拟时钟、后台提交、轮询线程。"""  # 与 V5.1 通过 submit() 集成

import json  # 校验 action 参数可由 JSON 持久化
import logging  # 定时线程发生错误时记录日志
import re  # 校验动作名称
from collections.abc import Callable  # 描述时钟与动作函数
from copy import deepcopy  # 每次执行时为动作提供独立参数副本
from dataclasses import dataclass, field  # 表示一次 tick 的执行结果
from datetime import datetime, timezone  # 标准化时间并计算漏执行间隔
from threading import Event, RLock, Thread  # 同步调度操作、停止后台定时线程
from typing import Any  # JSON 参数可以包含多种类型
from uuid import uuid4  # 生成计划唯一 ID

from devpilot.background.manager import BackgroundJobManager  # V5.1 后台任务管理器
from devpilot.background.model import JobContext  # V5.1 的协作式取消上下文
from devpilot.scheduler.cron import CronExpression  # 负责 Cron 解析与时间计算
from devpilot.scheduler.model import CronSchedule, MisfirePolicy  # 持久化数据结构
from devpilot.scheduler.store import CronStore  # 原子状态存储


CronAction = Callable[[JobContext, dict[str, Any]], str]  # 可重建动作：接收 JobContext 与持久化参数并返回文本
Clock = Callable[[], datetime]  # 注入时钟：测试中可以使用固定时间
ACTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")  # 防止非法动作名称与空名称
logger = logging.getLogger(__name__)  # 统一输出调度线程日志


@dataclass  # 记录一次到期检查的提交结果
class CronTickResult:  # 方便测试及 Lead 查询本轮调度行为
    started: dict[str, str] = field(default_factory=dict)  # schedule_id → job_id
    skipped: list[str] = field(default_factory=list)  # 因漏执行策略而跳过的计划 ID
    errors: dict[str, str] = field(default_factory=dict)  # 未找到动作或无法提交时的错误


class CronScheduler:  # 保存计划并负责把到期槽位提交到 BackgroundJobManager
    def __init__(self, store: CronStore, manager: BackgroundJobManager, clock: Clock | None = None, grace_seconds: float = 60.0):  # 注入存储、后台执行器和可选时钟
        if grace_seconds < 0:  # 迟到宽限不能为负
            raise ValueError("grace_seconds must be >= 0")  # 明确拒绝非法配置
        self.store = store  # 所有操作共享相同存储实例
        self.manager = manager  # 由 V5.1 负责真正的后台执行
        self.clock = clock or (lambda: datetime.now(timezone.utc))  # 默认取真实 UTC 时间
        self.grace_seconds = grace_seconds  # 超过该时间后，skip 策略将略过漏执行
        self._actions: dict[str, CronAction] = {}  # 保存可恢复的动作注册表
        self._lock = RLock()  # 防止 create、pause、run_due 同时修改状态
        self._stop_event = Event()  # 控制定时检查线程退出
        self._thread: Thread | None = None  # 调度线程尚未启动

    @staticmethod  # 统一把所有传入时间变成 UTC
    def _utc(value: datetime) -> datetime:  # 输入有时区的 datetime
        if value.tzinfo is None or value.utcoffset() is None:  # 拒绝时区不明的时间
            raise ValueError("Clock must provide timezone-aware datetimes")  # 防止时差错误
        return value.astimezone(timezone.utc)  # 使用 UTC 统一比较与持久化

    def _now(self) -> datetime:  # 取得当前时间
        return self._utc(self.clock())  # 支持真实时钟或测试 FakeClock

    def register_action(self, name: str, action: CronAction) -> None:  # 注册重启后可以重新绑定的动作名称
        if not isinstance(name, str) or not ACTION_NAME.fullmatch(name):  # 名称必须稳定且合法
            raise ValueError(f"Invalid cron action name: {name!r}")  # 避免存入随意函数描述
        if not callable(action):  # 动作必须能被实际调用
            raise TypeError("action must be callable")  # 拒绝不可调用对象
        with self._lock:  # 防止注册和调度竞争
            self._actions[name] = action  # 同名可以重新绑定，用于进程重启时恢复

    def create(self, name: str, expression: str, action: str, payload: dict[str, Any] | None = None, timezone_name: str = "UTC", misfire_policy: MisfirePolicy = "skip", now: datetime | None = None) -> CronSchedule:  # 创建并持久化一条定时计划
        if not isinstance(name, str) or not name.strip():  # 名称不能为空
            raise ValueError("Schedule name must not be empty")  # 校验计划名称
        if not isinstance(action, str) or not ACTION_NAME.fullmatch(action):  # 动作名称须能持久化并重新注册
            raise ValueError("Invalid action name")  # 拒绝无效名称
        if misfire_policy not in {"skip", "fire_once"}:  # 只支持两种明确的漏执行策略
            raise ValueError("misfire_policy must be skip or fire_once")  # 防止隐式行为
        arguments = {} if payload is None else payload  # 默认使用空参数
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):  # JSON 对象的键必须是字符串
            raise ValueError("payload must be a JSON object with string keys")  # 提前校验
        try:  # 确认传入参数可写入 JSON 文件
            serialized_payload = json.dumps(arguments, allow_nan=False)  # 禁止 NaN 和 Infinity
        except (TypeError, ValueError) as error:  # 不支持自定义 Python 对象持久化
            raise ValueError("payload must be JSON serializable") from error  # 将错误传给调用者
        with self._lock:  # 创建和到期检查使用同一把锁
            anchor = self._utc(now) if now is not None else self._now()  # 计算计划启用时刻
            next_time = CronExpression.parse(expression).next_after(anchor, timezone_name)  # 从当前时间寻找未来触发
            record = CronSchedule(id="cron_" + uuid4().hex[:12], name=name.strip(), expression=expression, action=action, payload=json.loads(serialized_payload), timezone=timezone_name, misfire_policy=misfire_policy, next_run_at=next_time.isoformat())  # 生成纯数据计划快照
            self.store.add(record)  # 保存后即使退出 Python 也不会丢失
            return record  # 返回完整计划信息

    def all(self) -> list[CronSchedule]:  # 查看所有定时计划
        return self.store.all()  # 直接调用持久化存储

    def pause(self, schedule_id: str) -> CronSchedule:  # 暂停未来触发
        with self._lock:  # 防止同一计划恰好被 tick 认领
            return self.store.update(schedule_id, enabled=False)  # 已提交的后台 Job 不会因此取消

    def resume(self, schedule_id: str, now: datetime | None = None) -> CronSchedule:  # 恢复计划，从当前时刻重新计算下一次触发
        with self._lock:  # 避免暂停与触发竞争
            record = self.store.get(schedule_id)  # 获取原计划
            if record is None:  # 不存在时明确报错
                raise KeyError(f"Unknown cron schedule: {schedule_id}")  # 拒绝不存在的 ID
            anchor = self._utc(now) if now is not None else self._now()  # 恢复时不补跑暂停期间的触发
            next_time = CronExpression.parse(record.expression).next_after(anchor, record.timezone)  # 找恢复之后的下一次
            return self.store.update(schedule_id, enabled=True, next_run_at=next_time.isoformat(), last_error=None)  # 重新启用并保存

    def remove(self, schedule_id: str) -> bool:  # 删除计划，不影响已经提交的后台 Job
        with self._lock:  # 防止删除时另一个线程正在提交
            return self.store.remove(schedule_id)  # 返回是否实际删除

    def run_due(self, now: datetime | None = None) -> CronTickResult:  # 检查当前时刻所有到期计划并提交后台 Job
        with self._lock:  # 保证同一个 Scheduler 内不会并发重复执行时间槽
            moment = self._utc(now) if now is not None else self._now()  # 取得固定的本轮检查时间
            result = CronTickResult()  # 收集触发、跳过和错误
            records = sorted(self.store.all(), key=lambda item: item.next_run_at or "")  # 先处理最早到期的计划
            for record in records:  # 逐条检查计划
                if not record.enabled or record.next_run_at is None:  # 只处理已经启用的计划
                    continue  # 跳过暂停项
                slot = datetime.fromisoformat(record.next_run_at)  # 解析原定触发时间
                if slot > moment:  # 原定时间还没有到
                    continue  # 保留给后面的 tick
                handler = self._actions.get(record.action)  # 从注册表找到动作函数
                if handler is None:  # 重启后可能还没注册动作
                    result.errors[record.id] = f"Unregistered action: {record.action}"  # 记录错误，不推进时间槽
                    continue  # 等动作注册完成后再处理
                try:  # 下一次触发可能找不到，需在认领前验证
                    next_slot = CronExpression.parse(record.expression).next_after(moment, record.timezone)  # 合并多次漏执行为下一次未来时间
                except Exception as error:  # 比如时区数据丢失或计划数据损坏
                    result.errors[record.id] = f"Cannot calculate next run: {error}"  # 留下诊断信息
                    continue  # 不丢弃当前时间槽
                claimed = self.store.claim_slot(record.id, record.next_run_at, next_slot.isoformat())  # 在磁盘上先认领该触发槽
                if not claimed:  # 其他操作已经修改此计划
                    continue  # 防止重复触发
                lateness = (moment - slot).total_seconds()  # 计算本轮检查比原定时间晚了多久
                if record.misfire_policy == "skip" and lateness > self.grace_seconds:  # 漏执行超出宽限时跳过
                    result.skipped.append(record.id)  # 记录跳过的计划
                    continue  # 不生成任何 Job
                args_copy = deepcopy(record.payload)  # 每次提交独立参数，防止处理器跨次污染
                def run_action(context: JobContext, selected: CronAction = handler, params: dict[str, Any] = args_copy) -> str:  # 包装成 V5.1 约定的单参数后台函数
                    return selected(context, params)  # 将时间计划动作交给后台线程执行
                try:  # 提交后台工作可能因 Manager 已关闭而失败
                    job_id = self.manager.submit(name=f"cron:{record.id}:{record.next_run_at}", function=run_action)  # Job 名称包含计划与时间槽，便于事后排查
                except Exception as error:  # 记录已认领但未成功提交的情况
                    message = f"{type(error).__name__}: {error}"  # 记录具体提交失败原因
                    self.store.record_submission(record.id, record.next_run_at, error=message)  # 写入诊断；不隐式重复提交
                    result.errors[record.id] = message  # 将错误告知调用者
                    continue  # 继续处理其他计划
                self.store.record_submission(record.id, record.next_run_at, job_id=job_id)  # 记录这次启动的后台任务 ID
                result.started[record.id] = job_id  # 本轮返回给 Lead 的提交结果
            return result  # 返回 started、skipped 与 errors

    def _loop(self, interval_seconds: float) -> None:  # 在专门的定时线程里运行
        while not self._stop_event.is_set():  # 只要还没收到停止请求
            try:  # 一个错误不应该永久杀死调度线程
                output = self.run_due()  # 检查并提交到期计划
                for schedule_id, message in output.errors.items():  # 遍历当前轮的错误
                    logger.warning("Cron schedule %s: %s", schedule_id, message)  # 输出可观测诊断
            except Exception:  # 对不期望的实现错误保留堆栈信息
                logger.exception("Cron scheduler tick failed")  # 下一轮继续尝试
            self._stop_event.wait(interval_seconds)  # 通过 Event 等待，stop() 可以立即唤醒

    def start(self, interval_seconds: float = 1.0) -> None:  # 开启真正的后台自动检查线程
        if interval_seconds <= 0:  # 不允许空转死循环
            raise ValueError("interval_seconds must be > 0")  # 明确提示
        with self._lock:  # 防止两个调用者同时启动
            if self._thread is not None and self._thread.is_alive():  # 已经运行则不重复启动
                raise RuntimeError("Cron scheduler is already running")  # 避免重复调度
            self._stop_event.clear()  # 允许新的执行周期
            self._thread = Thread(target=self._loop, args=(interval_seconds,), daemon=True, name="devpilot-cron")  # 创建专用调度线程
            self._thread.start()  # 后台开始周期性检查

    def stop(self, timeout: float | None = None) -> None:  # 停止调度线程而不是取消所有后台 Job
        self._stop_event.set()  # 立即唤醒当前等待的调度线程
        thread = self._thread  # 保存引用避免竞争
        if thread is not None and thread.is_alive():  # 只在确实运行时等待
            thread.join(timeout=timeout)  # 等待调度线程退出
            if thread.is_alive():  # 到期后线程仍在执行
                raise TimeoutError("Cron scheduler did not stop")  # 让调用方知道尚未完全停止
