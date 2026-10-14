from pathlib import Path  # 处理临时文件目录
from tempfile import TemporaryDirectory  # 演示时不污染项目根目录
from threading import Event  # 明确控制后台任务完成的时机

from devpilot.background.manager import BackgroundJobManager  # 复用 V5.1 的 Manager
from devpilot.background.store import JobStore  # 复用 V5.1 的 JobStore
from devpilot.events.bridge import make_job_event_callback, reconcile_terminal_jobs  # 把 Job 终态接到 EventBus
from devpilot.events.bus import EventBus  # 事件队列
from devpilot.events.dispatcher import EventDispatcher  # Lead 事件分发器
from devpilot.events.journal import EventJournal  # 事件持久化日志


def main() -> None:  # 演示 V5.2 完整执行链
    with TemporaryDirectory() as temporary:  # 创建结束后自动删除的演示目录
        root = Path(temporary)  # 将目录转成 Path 对象
        store = JobStore(root / "jobs.json")  # 后台 Job 持久化状态
        journal = EventJournal(root / "events.jsonl")  # 追加式事件记录
        bus = EventBus(journal)  # 启动时恢复未处理事件
        dispatcher = EventDispatcher(bus)  # 在 Lead 主线程处理通知
        release = Event()  # 控制任务什么时候结束

        def on_completed(event):  # 成功通知处理器
            job = store.get(event.job_id)  # 通过 ID 取得完整 Job 结果
            print(f"[Lead] completed: {event.job_id}, result={job.result}")  # 打印成功结果

        def on_failed(event):  # 失败通知处理器
            job = store.get(event.job_id)  # 从持久化状态取错误原因
            print(f"[Lead] failed: {event.job_id}, error={job.error}")  # 打印失败原因

        dispatcher.register("job.completed", on_completed)  # 注册成功回调
        dispatcher.register("job.failed", on_failed)  # 注册失败回调
        manager = BackgroundJobManager(store, max_workers=2, on_finished=make_job_event_callback(bus))  # 后台 Job 完成后发布事件
        reconcile_terminal_jobs(store, bus)  # 把之前漏发的终态事件补齐

        def successful_job(context):  # 模拟成功的后台任务
            assert release.wait(3)  # 在 Lead 发信号之前先等待
            return "3 tests passed"  # 返回最终结果

        def failing_job(context):  # 模拟失败的后台任务
            assert release.wait(3)  # 同样等 Lead 放行
            raise ValueError("synthetic test failure")  # 模拟测试失败

        try:  # 运行时必须正常关闭
            good_id = manager.submit("tests", successful_job)  # 提交成功任务，立即返回 ID
            bad_id = manager.submit("broken", failing_job)  # 提交失败任务，立即返回 ID
            print("[Lead] submitted:", good_id, bad_id)  # 提交之后 Lead 继续工作
            print("[Lead] still free to do other work")  # 此时两项任务还没结束
            release.set()  # 允许 Worker 完成
            manager.wait(good_id, timeout=3)  # 演示中显式等待，让输出顺序可复现
            manager.wait(bad_id, timeout=3)  # 确保失败任务也结束
            count = dispatcher.process_available()  # Lead 主动处理已到达的事件
            print("[Lead] dispatched:", count)  # 输出本次事件数量
            print("[Lead] unhandled:", len(journal.pending()))  # 应该没有未处理事件
        finally:  # 即使演示抛异常也清理资源
            release.set()  # 避免任务一直等待
            manager.shutdown()  # 正常关闭线程池


if __name__ == "__main__":  # 直接运行模块时执行演示
    main()  # 调用入口函数
