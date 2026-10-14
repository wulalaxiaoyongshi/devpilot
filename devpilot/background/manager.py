import logging  # 记录回调失败，避免影响原任务结果
from collections.abc import Callable  # 描述后台函数与完成回调的类型
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError  # Future、取消异常和线程池
from threading import Event, RLock  # 取消信号和临界区互斥
from uuid import uuid4  # 生成唯一 Job ID

from devpilot.background.model import BackgroundJob, JobCancelled, JobContext, JobStatus, utc_now  # Job 数据类型、状态类型和辅助函数
from devpilot.background.store import JobStore  # 线程安全持久化存储


JobFunction = Callable[[JobContext], str]  # 后台函数：接收 JobContext，返回字符串
FinishCallback = Callable[[BackgroundJob], None]  # 完成回调：接收最终 Job 状态
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}  # 不再运行的终态
logger = logging.getLogger(__name__)  # 模块日志器


class BackgroundJobManager:  # 负责后台执行、状态更新与取消
    def __init__(self, store: JobStore, max_workers: int = 4, on_finished: FinishCallback | None = None):  # 注入 Store、并发上限与可选回调
        if max_workers < 1:  # 拒绝非法并发数量
            raise ValueError("max_workers must be >= 1")  # 给调用者明确错误
        self.store = store  # 所有线程共用同一个 Store 对象
        self.store.recover_interrupted()  # 将上次进程遗留的未完成状态标记为 interrupted
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="devpilot-bg")  # 创建长期复用的线程池
        self.on_finished = on_finished  # 留给 V5.2 的 EventBus 接入点
        self._lock = RLock()  # 保护 Future 字典和取消操作
        self._futures: dict[str, Future] = {}  # 保存 Job ID 到 Future 的对应关系
        self._cancel_events: dict[str, Event] = {}  # 保存 Job ID 到取消信号的对应关系
        self._closed = False  # 阻止关闭后继续提交新任务

    def submit(self, name: str, function: JobFunction, task_id: str | None = None) -> str:  # 提交任务并立即返回 Job ID
        if not name.strip():  # 名称不能为空
            raise ValueError("Job name must not be empty")  # 明确拒绝无效参数
        if not callable(function):  # 防止传入不可执行对象
            raise TypeError("function must be callable")  # 提前报错
        with self._lock:  # 保证提交过程中管理器状态一致
            if self._closed:  # 运行时关闭后不再接单
                raise RuntimeError("BackgroundJobManager is shut down")  # 直接报错
            job_id = "job_" + uuid4().hex[:12]  # 为本次执行生成唯一标识
            cancel_event = Event()  # 每个任务有自己独立的取消信号
            self.store.add(BackgroundJob(id=job_id, name=name, task_id=task_id))  # 先持久化 pending，再启动线程
            self._cancel_events[job_id] = cancel_event  # 保存信号供 cancel() 使用
            future = self.executor.submit(self._run, job_id, function, cancel_event)  # 提交任务，Future 立即返回
            self._futures[job_id] = future  # 保存 Future 便于查询与等待
            return job_id  # 不等待后台任务结束

    def _run(self, job_id: str, function: JobFunction, cancel_event: Event) -> None:  # 在线程池中运行的包装函数
        with self._lock:  # 和 cancel() 协调状态转换
            current = self.store.get(job_id)  # 读取最新 Job 状态
            if current is None or current.status == "cancelled":  # 已被提前取消则不执行
                return  # 结束包装函数
            status = "cancelling" if cancel_event.is_set() else "running"  # 启动时如已收到取消，保持取消中状态
            self.store.update(job_id, status=status, started_at=utc_now())  # 保存启动时间和状态
        context = JobContext(job_id, cancel_event)  # 为用户后台函数创建上下文
        try:  # 开始执行用户后台函数
            context.raise_if_cancelled()  # 如果启动前取消，则立即退出
            result = function(context)  # 真正执行用户传入的工作
            if not isinstance(result, str):  # V5.1 约定结果统一为字符串
                raise TypeError("Background function must return str")  # 非字符串算执行失败
        except JobCancelled as error:  # 后台函数承认并响应了取消
            final = self._finish(job_id, status="cancelled", error=str(error))  # 保存取消终态
        except Exception as error:  # 后台任务出现任何普通异常
            final = self._finish(job_id, status="failed", error=f"{type(error).__name__}: {error}")  # 保存失败原因
        else:  # 后台函数正常返回
            final = self._finish(job_id, status="completed", result=result)  # 即使取消曾被请求，未协作退出的函数仍算完成
        self._notify(final)  # 通知完成回调；V5.2 将在这里发布事件

    def _finish(self, job_id: str, status: JobStatus, result: str | None = None, error: str | None = None) -> BackgroundJob:  # 统一保存终态
        with self._lock:  # 终态写入期间锁住状态
            return self.store.update(job_id, status=status, result=result, error=error, finished_at=utc_now())  # 返回已持久化的新快照

    def get_status(self, job_id: str) -> BackgroundJob:  # 非阻塞查询状态
        job = self.store.get(job_id)  # 从持久化层取状态
        if job is None:  # 找不到任务时明确报错
            raise KeyError(f"Unknown job: {job_id}")  # 不假装是 pending
        return job  # 返回完整任务状态快照

    def wait(self, job_id: str, timeout: float | None = None) -> BackgroundJob:  # 显式等待某个任务结束
        self.get_status(job_id)  # 首先确认任务存在
        with self._lock:  # 只在读取 Future 引用时加锁
            future = self._futures.get(job_id)  # 取得当前进程内的 Future
        if future is None:  # 重启后的历史任务没有 Future
            return self.get_status(job_id)  # 直接返回已存储的状态
        try:  # Future.result() 可以可靠处理尚未启动就被取消的 Future
            future.result(timeout=timeout)  # 等待完成；包装函数异常会向调用方传播
        except CancelledError:  # 如果 Future 在开始执行前被取消
            pass  # 状态已经在 cancel() 中写入 cancelled
        except FutureTimeoutError as error:  # 指定时间内仍未结束
            raise TimeoutError(f"Job {job_id} did not finish in time") from error  # 超时不终止后台执行
        return self.get_status(job_id)  # 返回最新持久化状态

    def get_result(self, job_id: str) -> str:  # 获取已完成的文本结果
        job = self.get_status(job_id)  # 查询当前状态
        if job.status == "completed":  # 只有成功终态才能返回结果
            return job.result or ""  # 空字符串也是合法成功结果
        if job.status == "cancelled":  # 任务已经取消
            raise JobCancelled(job.error or f"Job {job_id} cancelled")  # 明确区分取消与失败