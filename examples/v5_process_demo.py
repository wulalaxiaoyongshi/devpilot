"""V5.3 实际运行示例：启动后台 Python 子进程并消费完成事件。"""  # 可以用 python -m examples.v5_process_demo 执行

import sys  # 用当前解释器生成跨平台命令
from pathlib import Path  # 指定演示工作目录
from tempfile import TemporaryDirectory  # 避免污染真实 Git 项目

from devpilot.background.manager import BackgroundJobManager  # V5.1 后台线程池
from devpilot.background.process import BackgroundProcessRunner, ProcessSpec  # V5.3 实际 OS 进程管理
from devpilot.background.store import JobStore  # V5.1 持久化状态
from devpilot.events.bridge import make_job_event_callback  # V5.2 事件桥接
from devpilot.events.bus import EventBus  # V5.2 事件队列
from devpilot.events.dispatcher import EventDispatcher  # V5.2 分发器
from devpilot.events.journal import EventJournal  # V5.2 持久化事件


def main() -> None:  # 演示 V5.1 + V5.2 + V5.3 完整数据流
    with TemporaryDirectory() as temporary:  # 自动创建和清理演示目录
        workspace = Path(temporary)  # 转换为 Path
        store = JobStore(workspace / "jobs.json")  # Job 状态持久化路径
        journal = EventJournal(workspace / "events.jsonl")  # Event 记录路径
        bus = EventBus(journal)  # 事件总线
        dispatcher = EventDispatcher(bus)  # 由 Lead 主线程消费事件
        manager = BackgroundJobManager(store, max_workers=2, on_finished=make_job_event_callback(bus))  # 后台执行与事件桥接
        runner = BackgroundProcessRunner(workspace)  # 限制进程工作目录
        dispatcher.register("job.completed", lambda event: print("[Lead] completed:", event.job_id))  # 注册成功处理器
        dispatcher.register("job.failed", lambda event: print("[Lead] failed:", event.job_id))  # 注册失败处理器
        try:  # 确保运行完后关闭线程池
            good_spec = ProcessSpec(argv=(sys.executable, "-c", "print('3 tests passed')"))  # 构造成功的模拟测试命令
            bad_spec = ProcessSpec(argv=(sys.executable, "-c", "import sys; print('1 test failed', file=sys.stderr); sys.exit(2)"))  # 构造失败的模拟测试命令
            good_id = runner.submit(manager, "tests-pass", good_spec)  # 提交后立即返回 ID
            bad_id = runner.submit(manager, "tests-fail", bad_spec)  # 第二个进程并行执行
            print("[Lead] submitted:", good_id, bad_id)  # Lead 已经能继续工作
            print("[Lead] not blocked; work continues")  # 证明提交不等待结果
            good = manager.wait(good_id, timeout=10)  # 演示中主动等待，以稳定输出顺序
            bad = manager.wait(bad_id, timeout=10)  # 等待第二个进程结束
            dispatcher.process_available()  # 消费后台产生的完成/失败事件
            print("[Lead] good status:", good.status)  # 显示完成状态
            print("[Lead] good output:\n", good.result)  # 包含输出预览和完整日志路径
            print("[Lead] bad status:", bad.status)  # 显示失败状态
            print("[Lead] bad error:\n", bad.error)  # 包含退出码、错误预览及日志路径
        finally:  # 即使运行出错也退出进程管理环境
            manager.shutdown()  # 等待线程池清理


if __name__ == "__main__":  # 只在 python -m 执行时运行
    main()  # 调用演示入口
