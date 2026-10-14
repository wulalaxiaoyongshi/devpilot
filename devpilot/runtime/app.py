"""V5.7：用户可以直接运行的统一 DevPilot 应用入口。"""  # 管理 Lead 会话和后台事件生命周期

from pathlib import Path  # 用于暴露持久化目录
from threading import RLock  # 保证 Lead.run 不被两个线程同时进入
from typing import Any  # 复用 V0～V5 具体对象

from devpilot.runtime.lead_delivery import DeliveryResult, LeadDelivery  # 事件→Lead 投递逻辑


class DevPilotApp:  # 单进程 DevPilot Runtime 的入口
    def __init__(self, lead: Any, delivery: LeadDelivery, runtime: Any, jobs: Any, cron: Any, team: Any, state_dir: Path, lead_lock: RLock):  # 接受 Factory 组装的共享组件
        self.lead = lead  # 真实 V0 Lead Agent
        self.delivery = delivery  # V5.7 可靠事件投递器
        self.runtime = runtime  # V5.5 EventTeamRuntime
        self.jobs = jobs  # V5.1 后台 Job 管理
        self.cron = cron  # V5.4 Cron 调度
        self.team = team  # V4 TeamManager
        self.state_dir = state_dir  # 持久化状态根目录
        self._lead_lock = lead_lock  # 对外 chat 和 pump 共用一个 Lead 锁
        self._closed = False  # 防止关闭后继续使用

    def chat(self, text: str) -> str:  # 用户输入一轮任务，真实经过 V0 Agent Loop
        with self._lead_lock:  # 防止和后台事件回传同时修改 Lead.messages
            if self._closed:  # Runtime 已关闭
                raise RuntimeError("DevPilotApp is closed")  # 禁止继续使用
            return self.lead.run(text)  # V0→V1→V5，普通聊天和工具调用原样进行

    def pump(self, resume_lead: bool = True) -> DeliveryResult:  # 由宿主主线程周期性调用，取事件并让 Lead 继续
        with self._lead_lock:  # 与 chat 使用相同的互斥锁
            if self._closed:  # 关闭后不消费
                raise RuntimeError("DevPilotApp is closed")  # 抛出明确错误
            return self.delivery.pump(resume_lead=resume_lead)  # 由 V5.2 Dispatcher 推入 V0 Lead Agent.run

    def create_teammate(self, agent_id: str, role: str) -> Any:  # 为脚本调用提供一个明确入口
        if self._closed:  # 关闭状态不允许生成新 Agent
            raise RuntimeError("DevPilotApp is closed")  # 报告异常
        return self.team.add_teammate(agent_id, role)  # 由 V4 真正创建 Git Worktree 和 Agent

    def start_cron(self, interval_seconds: float = 1.0) -> None:  # 启动 V5.4 的后台定时检测
        if self._closed:  # 关闭状态不能再启动调度器
            raise RuntimeError("DevPilotApp is closed")  # 拒绝
        self.cron.start(interval_seconds=interval_seconds)  # 使用 V5.4 原有 start 接口

    def shutdown(self) -> None:  # 按固定顺序关闭后台资源
        with self._lead_lock:  # 避免关闭与 Lead.run 重叠
            if self._closed:  # 允许重复关闭
                return  # 不重复调用 shutdown
            self._closed = True  # 阻止新入口提交任务
            self.runtime.shutdown(self.cron)  # V5.5 先停止 Cron、再等 Worker、最后处理事件
