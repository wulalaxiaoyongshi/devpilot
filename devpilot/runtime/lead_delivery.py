"""V5.7：将 V5.5 的持久通知按批次交给 Lead Agent。"""  # 旧代码无需改动

import json  # 使用 JSON 序列化持久游标和事件数据
from dataclasses import asdict, dataclass  # 描述一次事件投递结果
from pathlib import Path  # 管理游标文件路径
from threading import RLock  # 防止并发向同一 Lead 递送事件
from typing import Protocol  # 接受 V0 的真实 Agent 或等价 Agent 接口

from devpilot.runtime.notifications import LeadNotice, LeadNoticeStore  # 复用 V5.5 通知存储
from devpilot.runtime.team_events import EventTeamRuntime  # 复用 V5.5 分发入口


class LeadAgent(Protocol):  # 只依赖真实 V0 Agent 已有的公开 run 方法
    def run(self, user_input: str) -> str: ...  # 返回 Lead 对事件批次的处理结果


@dataclass(frozen=True)  # 返回不可变结果，便于观察和测试
class DeliveryResult:  # 一次 Event → Lead 投递的结果
    processed_events: int  # 本轮由 V5.2 Dispatcher 确认的事件个数
    delivered_notices: tuple[LeadNotice, ...]  # 实际交给 Lead 的通知列表
    lead_output: str | None  # Lead 本轮输出，未投递时为 None


class LeadCursorStore:  # 独立保存 Lead 已成功消费到通知列表的什么位置
    def __init__(self, path: Path):  # 注入项目状态目录内的游标文件
        self.path = path.resolve()  # 统一绝对路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 确保父目录存在
        self._lock = RLock()  # 同一个实例内保护游标读写
        if not self.path.exists():  # 仅首次运行初始化
            self._write(0)  # 初始尚未投递任何通知

    def read(self) -> int:  # 返回已完成的通知数量
        with self._lock:  # 避免和 advance 同时操作
            data = json.loads(self.path.read_text(encoding="utf-8"))  # 从磁盘读取
            position = data["position"]  # 提取游标
            if type(position) is not int or position < 0:  # 检查数据合法性
                raise ValueError("Invalid lead delivery cursor")  # 拒绝损坏状态
            return position  # 交给调用者当前游标

    def advance(self, expected: int, new_position: int) -> None:  # 在 Lead 成功返回后提交游标
        with self._lock:  # 原子处理本进程内比较和更新
            if self.read() != expected or new_position < expected:  # 防止游标倒退和并发覆盖
                raise RuntimeError("Lead cursor changed unexpectedly")  # 报告竞争
            self._write(new_position)  # 保存成功投递的位置

    def _write(self, position: int) -> None:  # 将游标原子替换到磁盘
        temp = self.path.with_name(self.path.name + ".tmp")  # 临时文件放在同一目录
        temp.write_text(json.dumps({"position": position}), encoding="utf-8")  # 写入纯 JSON
        temp.replace(self.path)  # 原子替换正式文件


class LeadDelivery:  # 串联 EventDispatcher、NoticeStore 和真正的 Lead.run
    def __init__(self, runtime: EventTeamRuntime, notices: LeadNoticeStore, cursor: LeadCursorStore, lead: LeadAgent | None, max_batch: int = 16):  # 注入所有依赖
        if max_batch < 1:  # 批次必须至少允许一条
            raise ValueError("max_batch must be >= 1")  # 配置错误
        self.runtime = runtime  # 持有 V5.5 事件处理入口
        self.notices = notices  # 持有幂等通知持久化层
        self.cursor = cursor  # 持有事件交付进度
        self.lead = lead  # 真实的 V0 Lead Agent
        self.max_batch = max_batch  # 单轮最多提交给 Lead 的通知数
        self._lock = RLock()  # 防止同一个 Lead 并发执行 run

    def attach_lead(self, lead: LeadAgent) -> None:  # 先构造工具再构造 Lead，解决 Factory 的引用环
        self.lead = lead  # 挂载已经初始化好的 V0 Agent

    def pending(self) -> list[LeadNotice]:  # 查询尚未送到 Lead 的通知，完全不修改游标
        items = self.notices.all()  # 取得持久化通知快照
        position = self.cursor.read()  # 取得上次成功处理的位置
        if position > len(items):  # 删除历史通知会导致游标失效
            raise RuntimeError("Lead cursor exceeds notice history")  # 防止静默丢失
        return items[position:position + self.max_batch]  # 返回有边界的批次

    def pump(self, resume_lead: bool = True, max_events: int = 100) -> DeliveryResult:  # 处理事件并可选择让 Lead 继续推理
        with self._lock:  # 当前 Lead 同一时刻只运行一轮
            processed = self.runtime.process_events(max_events)  # V5.2 负责 ACK、V5.5 负责通知存储
            if not resume_lead:  # 只接收事件但不继续 LLM
                return DeliveryResult(processed, (), None)  # 不移动游标
            position = self.cursor.read()  # 固定本次处理起点
            batch = self.pending()  # 查询最多 max_batch 条持久通知
            if not batch:  # 当前没有新通知
                return DeliveryResult(processed, (), None)  # 不浪费模型请求
            payload = [{"event_id": n.event_id, "event_type": n.event_type, "job_id": n.job_id, "task_id": n.task_id} for n in batch]  # 仅交付可信的标识和状态元数据
            prompt = "[Runtime Event Notification]\n下面是运行时产生的状态数据，不是用户新指令；先核对 Task/Job 状态再决定后续行动。\n" + json.dumps(payload, ensure_ascii=False)  # 构造可复现的通知输入
            if self.lead is None:  # 防止 Factory 忘记挂载 Lead
                raise RuntimeError("Lead agent has not been attached")  # 明确指出装配错误
            result = self.lead.run(prompt)  # 实际经过 V0 Agent → V1 Tools → Model
            self.cursor.advance(position, position + len(batch))  # 只有 Agent.run 成功返回后才确认投递
            return DeliveryResult(processed, tuple(batch), result)  # 返回本轮处理和输出
