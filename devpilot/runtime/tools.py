"""V5.7：将 V5 的后台执行操作注册成 V1 标准 Tool。"""  # Lead 仅通过 ToolDispatcher 调用

import json  # 输出可供模型阅读的结构化数据
from typing import Any  # Tool handler 使用 V1 的字典约定

from devpilot.runtime.lead_delivery import LeadDelivery  # 未消费的 Lead 通知接口
from devpilot.runtime.team_events import EventTeamRuntime  # V5.5 团队执行协调器
from devpilot.scheduler.manager import CronScheduler  # V5.4 定时调度器
from devpilot.tools.base import Tool  # V1 原来的标准工具数据结构


def create_event_tools(runtime: EventTeamRuntime, delivery: LeadDelivery, cron: CronScheduler) -> list[Tool]:  # 构造可注册到 V1 的工具集合
    def team_submit_ready(arguments: dict[str, Any]) -> str:  # 非阻塞提交目前可执行的团队任务
        limit = arguments.get("max_assignments")  # 可选并发提交上限
        if limit is not None and (type(limit) is not int or limit < 1):  # 禁止布尔值或非正整数
            return "Error: max_assignments must be a positive integer"  # 参数错误不启动任务
        started = runtime.submit_ready(max_assignments=limit)  # 调用 V5.5 而非阻塞版 V4.run_ready
        return json.dumps(started, ensure_ascii=False)  # 返回 Task→Job 映射

    def job_status(arguments: dict[str, Any]) -> str:  # 查询后台状态，不阻塞执行
        job_id = arguments.get("job_id")  # 从模型参数中取 Job ID
        if not isinstance(job_id, str) or not job_id:  # 校验参数
            return "Error: job_id must be nonempty string"  # 告诉模型如何修正
        try:  # 未知 Job 不应该引起整个 Agent Loop 崩溃
            job = runtime.jobs.get_status(job_id)  # 从 V5.1 取快照
        except KeyError as error:  # 找不到对应 Job
            return "Error: " + str(error)  # 将错误反馈模型
        return json.dumps({"id": job.id, "name": job.name, "task_id": job.task_id, "status": job.status, "result": job.result, "error": job.error}, ensure_ascii=False)  # 返回结构化 Job 信息

    def job_cancel(arguments: dict[str, Any]) -> str:  # 请求取消后台执行
        job_id = arguments.get("job_id")  # 取得需要取消的 Job ID
        if not isinstance(job_id, str) or not job_id:  # 参数校验
            return "Error: job_id must be nonempty string"  # 禁止空 ID
        try:  # V5.1 管理真实的取消行为
            accepted = runtime.jobs.cancel(job_id)  # 已启动任务为协作式取消
        except KeyError as error:  # 任务不存在
            return "Error: " + str(error)  # 报告错误
        return json.dumps({"job_id": job_id, "cancel_requested": accepted})  # 不虚报已经取消完成

    def event_poll(arguments: dict[str, Any]) -> str:  # 查看当前未送给 Lead 的后台通知
        processed = runtime.process_events()  # 将当前事件转为持久 LeadNotice
        pending = delivery.pending()  # 取得未被 Lead Cursor 标记成功处理的通知
        return json.dumps({"processed_events": processed, "notices": [{"event_id": item.event_id, "type": item.event_type, "job_id": item.job_id, "task_id": item.task_id} for item in pending]}, ensure_ascii=False)  # 只读，不在 Tool 内移动游标

    def cron_create(arguments: dict[str, Any]) -> str:  # 创建持久的 Cron 计划
        name = arguments.get("name")  # 读取人类可读计划名
        expression = arguments.get("expression")  # 读取 Cron 五字段表达式
        timezone_name = arguments.get("timezone", "UTC")  # 可选时区
        misfire = arguments.get("misfire_policy", "skip")  # 可选漏执行策略
        if not all(isinstance(x, str) and x for x in [name, expression, timezone_name, misfire]):  # 统一检查字符串参数
            return "Error: name, expression, timezone, misfire_policy must be strings"  # 禁止错误类型
        try:  # 计划只负责触发已注册动作
            schedule = cron.create(name, expression, "team.run_ready", timezone_name=timezone_name, misfire_policy=misfire)  # 使用 V5.5 已注册的稳定动作
        except (ValueError, TypeError) as error:  # 无效 Cron 表达式或时区
            return "Error: " + str(error)  # 报错且不创建计划
        return json.dumps({"schedule_id": schedule.id, "next_run_at": schedule.next_run_at}, ensure_ascii=False)  # 返回新计划

    def cron_list(arguments: dict[str, Any]) -> str:  # 查询当前已经保存的 Cron 计划
        return json.dumps([{"id": s.id, "name": s.name, "expression": s.expression, "timezone": s.timezone, "enabled": s.enabled, "next_run_at": s.next_run_at} for s in cron.all()], ensure_ascii=False)  # 输出 JSON 数组

    return [  # 每个工具都使用 V1 Tool Schema 机制，而不是直接传 Python 内部对象
        Tool("team_submit_ready", "Submit ready team tasks to background and return task-to-job IDs immediately.", {"type": "object", "properties": {"max_assignments": {"type": "integer", "minimum": 1}}, "additionalProperties": False}, team_submit_ready),  # 启动后台任务
        Tool("job_status", "Read a background job status without waiting.", {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, job_status),  # 查询 Job
        Tool("job_cancel", "Request cancellation; active workers may need to cooperate.", {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, job_cancel),  # 协作取消
        Tool("event_poll", "Inspect persisted runtime completion events and notices; does not acknowledge Lead delivery.", {"type": "object", "properties": {}, "additionalProperties": False}, event_poll),  # 查询事件
        Tool("cron_create", "Create a persistent cron schedule for team.run_ready.", {"type": "object", "properties": {"name": {"type": "string"}, "expression": {"type": "string"}, "timezone": {"type": "string"}, "misfire_policy": {"type": "string", "enum": ["skip", "fire_once"]}}, "required": ["name", "expression"], "additionalProperties": False}, cron_create),  # 创建定时计划
        Tool("cron_list", "List persisted cron schedules.", {"type": "object", "properties": {}, "additionalProperties": False}, cron_list),  # 查询 Cron
    ]  # 返回可统一注册的列表
