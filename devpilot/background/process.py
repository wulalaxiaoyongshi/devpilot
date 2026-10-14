"""V5.3：安全边界明确的后台子进程执行器。"""  # 本模块只管理进程，不修改 V5.1 的 Job 状态机

import os  # 识别 Windows / POSIX 平台
import signal  # POSIX 进程组终止信号
import subprocess  # 创建与监控操作系统进程
from dataclasses import dataclass  # 声明进程输入和结果
from pathlib import Path  # 管理工作目录与日志路径
from time import monotonic  # 不受系统时钟修改影响的计时器
from typing import Callable  # 声明后台函数接口
from uuid import uuid4  # 为每次运行创建独立日志文件名

from devpilot.background.manager import BackgroundJobManager  # 复用 V5.1 的线程池和 Job 生命周期
from devpilot.background.model import JobCancelled, JobContext  # 复用 V5.1 的协作取消协议


@dataclass(frozen=True)  # 输入配置在执行过程中不可修改
class ProcessSpec:  # 描述一次操作系统子进程执行
    argv: tuple[str, ...]  # 程序和参数，例如 ("python", "-m", "pytest")；不经过 Shell
    cwd: Path | None = None  # 子进程工作目录，默认 Runner 的 workspace
    timeout_seconds: float = 120.0  # 最长运行时间，必须大于 0
    poll_interval: float = 0.05  # 检查取消或超时的最长间隔
    grace_seconds: float = 0.5  # POSIX 发送 SIGTERM 后等待的时间
    preview_bytes: int = 4096  # 单个输出流的最大预览字节数


@dataclass(frozen=True)  # 保存一次已正常退出的进程结果
class ProcessResult:  # 独立于 V5.1 的 BackgroundJob
    argv: tuple[str, ...]  # 实际启动的参数数组
    cwd: str  # 实际执行目录
    exit_code: int  # 进程退出码；0 表示命令层执行成功
    stdout_path: str  # 完整标准输出文件
    stderr_path: str  # 完整标准错误文件
    stdout_preview: str  # 长输出的首尾预览
    stderr_preview: str  # 错误输出的首尾预览
    duration_seconds: float  # 本次进程运行耗时

    def summary(self) -> str:  # 将结构化结果转换成 V5.1 所需的 str
        return "\n".join([f"Command: {self.argv!r}", f"Exit code: {self.exit_code}", f"Duration: {self.duration_seconds:.3f}s", f"Stdout:\n{self.stdout_preview}", f"Stderr:\n{self.stderr_preview}", f"Stdout log: {self.stdout_path}", f"Stderr log: {self.stderr_path}"])  # 返回包含完整日志路径的限长文本


class ProcessExecutionError(RuntimeError):  # 子进程执行完但退出码非零
    def __init__(self, result: ProcessResult):  # 保留结构化结果以便日志与测试访问
        self.result = result  # 外部代码可以读取 exit_code、stdout_path 和 stderr_path
        super().__init__(f"Process exited with code {result.exit_code}\n{result.summary()}")  # V5.1 将这段说明持久化到 Job.error


class ProcessTimeoutError(TimeoutError):  # 子进程超过指定超时时间
    pass  # 具体错误描述在 run() 中生成


class BackgroundProcessRunner:  # V5.1 后台 Job 与操作系统进程之间的桥梁
    def __init__(self, workspace_root: Path, output_root: Path | None = None):  # 指定允许执行的项目工作区及日志目录
        self.workspace_root = workspace_root.resolve(strict=True)  # 保证工作区确实存在
        if not self.workspace_root.is_dir():  # 禁止使用普通文件作为工作区
            raise ValueError("workspace_root must be a directory")  # 提前拒绝错误配置
        requested_output = output_root or self.workspace_root / ".devpilot" / "processes"  # 默认存放到被 Git 忽略的运行目录
        if not requested_output.resolve().is_relative_to(self.workspace_root):  # 创建目录之前先校验解析后的目标位置
            raise ValueError("output_root must stay inside workspace_root")  # 不允许在工作区外创建输出目录
        requested_output.mkdir(parents=True, exist_ok=True)  # 仅在检查通过后创建日志目录
        self.output_root = requested_output.resolve(strict=True)  # 创建后再次解析软链接与真实目录
        if not self.output_root.is_relative_to(self.workspace_root):  # 防止创建过程中路径被替换成外部软链接
            raise ValueError("output_root must stay inside workspace_root")  # 不接受工作区外输出路径

    def _validate(self, spec: ProcessSpec) -> Path:  # 验证启动参数和路径
        if not isinstance(spec.argv, tuple) or not spec.argv:  # 必须使用非空参数元组
            raise ValueError("argv must be a non-empty tuple")  # 避免把整条 Shell 命令字符串传入
        if any(not isinstance(arg, str) or "\x00" in arg for arg in spec.argv):  # 不允许非字符串和 NUL 参数
            raise ValueError("argv must contain only valid strings")  # 提前拒绝异常输入
        if not spec.argv[0]:  # 程序名不能是空串
            raise ValueError("argv[0] must be a program")  # 明确错误位置
        if spec.timeout_seconds <= 0 or spec.poll_interval <= 0 or spec.grace_seconds < 0:  # 计时参数必须有效
            raise ValueError("Invalid timeout or polling settings")  # 防止死循环或负超时
        if spec.preview_bytes < 64:  # 预览需要保留一定上下文
            raise ValueError("preview_bytes must be >= 64")  # 拒绝极端小值
        cwd = (spec.cwd or self.workspace_root).resolve(strict=True)  # 规范化当前进程工作目录
        if not cwd.is_dir() or not cwd.is_relative_to(self.workspace_root):  # 禁止越过工作区或使用文件作为目录
            raise ValueError("Process cwd must be an existing directory inside workspace_root")  # 报告路径越界
        return cwd  # 输出经过验证的工作目录

    @staticmethod  # 不依赖实例状态的输出辅助方法
    def _preview(path: Path, limit: int) -> str:  # 仅读取文件首尾以控制内存占用
        total = path.stat().st_size  # 取得输出文件字节数
        with path.open("rb") as stream:  # 以二进制模式读取，避免多字节文本的定位误差
            if total <= limit:  # 短输出完整读取
                data = stream.read()  # 整个文件不会超过预览限制
                return data.decode("utf-8", errors="replace")  # 解码非法字节时不使 Job 失败
            head_size = limit // 2  # 分配一半预算给输出前部
            tail_size = limit - head_size  # 另一半预算保留输出结尾和错误汇总
            head = stream.read(head_size)  # 读取开头固定数量字节
            stream.seek(-tail_size, os.SEEK_END)  # 跳转到文件末尾前的固定位置
            tail = stream.read(tail_size)  # 读取最后固定数量字节
        omitted = total - limit  # 计算省略的字节数
        return head.decode("utf-8", errors="replace") + f"\n...[omitted {omitted} bytes]...\n" + tail.decode("utf-8", errors="replace")  # 返回首尾预览

    @staticmethod  # 跨平台尽力结束整个进程树
    def _terminate_tree(process: subprocess.Popen, grace_seconds: float) -> None:  # 处理 timeout 或 cancellation
        if os.name == "nt":  # Windows 没有 POSIX os.killpg
            try:  # 使用系统 taskkill 结束目标 PID 和子进程
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=max(3.0, grace_seconds + 1.0), check=False)  # /T 尝试结束整棵树
            except (OSError, subprocess.TimeoutExpired):  # taskkill 不可用或失败时回退
                pass  # 下方仍尝试直接杀死父进程
            if process.poll() is None:  # 父进程仍然存活
                process.kill()  # 直接终止父进程以保证 wait() 有机会结束
        else:  # POSIX 子进程由 start_new_session 创建独立进程组
            try:  # 先温和地请求组内进程退出
                os.killpg(process.pid, signal.SIGTERM)  # 发送 SIGTERM 给父进程和子进程
            except ProcessLookupError:  # 进程组可能已经退出
                pass  # 无需额外处理
            try:  # 给进程最多 grace_seconds 秒处理退出
                process.wait(timeout=grace_seconds)  # 等待组长进程退出
            except subprocess.TimeoutExpired:  # 进程没有在宽限期内退出
                pass  # 下一步强制清理整个进程组
            try:  # 即使父进程已退出，仍尝试清理组内残留子进程
                os.killpg(process.pid, signal.SIGKILL)  # 向原进程组发送 SIGKILL
            except ProcessLookupError:  # 进程组已经清空
                pass  # 不算异常
        try:  # 无论哪个平台都回收父进程
            process.wait(timeout=max(1.0, grace_seconds + 0.5))  # 避免留下当前 Python 进程可回收的僵尸子进程
        except subprocess.TimeoutExpired:  # 极端情况下父进程仍未终止
            process.kill()  # 再次强制终止父进程
            process.wait(timeout=2.0)  # 等待父进程被系统回收

    def run(self, context: JobContext, spec: ProcessSpec) -> ProcessResult:  # 同步执行一个操作系统进程，供后台线程调用
        cwd = self._validate(spec)  # 启动前验证目录与输入参数
        context.raise_if_cancelled()  # 在真正创建子进程前响应已有取消请求
        run_id = "process_" + uuid4().hex  # 为本次执行生成独立日志前缀
        stdout_path = self.output_root / f"{run_id}.stdout.log"  # 标准输出文件
        stderr_path = self.output_root / f"{run_id}.stderr.log"  # 标准错误文件
        start = monotonic()  # 记录启动前的单调时间
        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:  # 使用独占创建方式避免误覆盖日志
            process = subprocess.Popen(list(spec.argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file, shell=False, start_new_session=os.name != "nt", creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)  # 不经过 Shell，并为进程树清理创建隔离组
            try:  # 轮询子进程结束、取消以及超时
                while True:  # 直到子进程产生终态
                    exit_code = process.poll()  # 非阻塞检查退出码
                    if exit_code is not None:  # 进程已经退出
                        break  # 跳出轮询进入输出解析
                    context.raise_if_cancelled()  # 当 Lead 请求取消时抛出 JobCancelled
                    remaining = spec.timeout_seconds - (monotonic() - start)  # 计算剩余执行时间
                    if remaining <= 0:  # 超过进程运行预算
                        raise ProcessTimeoutError(f"Process timed out after {spec.timeout_seconds}s; logs: {stdout_path}, {stderr_path}")  # 通知 BackgroundJobManager 标记失败
                    try:  # 在短时间内等待退出，避免空转占 CPU
                        process.wait(timeout=min(spec.poll_interval, remaining))  # 最多等待一个轮询周期
                    except subprocess.TimeoutExpired:  # 子进程仍在执行
                        continue  # 下轮继续检查是否取消和超时
            except BaseException:  # 超时、取消、KeyboardInterrupt 等情况下必须回收子进程
                self._terminate_tree(process, spec.grace_seconds)  # 清理进程树，防止任务取消但命令仍在后台运行
                raise  # 将原因交给 V5.1 捕获并更新 Job
        result = ProcessResult(argv=spec.argv, cwd=str(cwd), exit_code=exit_code, stdout_path=str(stdout_path), stderr_path=str(stderr_path), stdout_preview=self._preview(stdout_path, spec.preview_bytes), stderr_preview=self._preview(stderr_path, spec.preview_bytes), duration_seconds=monotonic() - start)  # 构造结构化退出结果
        return result  # 允许上层根据退出码选择成功或失败语义

    def as_job_function(self, spec: ProcessSpec) -> Callable[[JobContext], str]:  # 适配 V5.1 JobFunction 签名
        self._validate(spec)  # 提交前先执行确定性校验，避免把配置错误当成后台异常
        def execute(context: JobContext) -> str:  # 由 V5.1 的线程池在后台调用
            result = self.run(context, spec)  # 等待真实 OS 进程执行结束
            if result.exit_code != 0:  # 命令返回非零退出码
                raise ProcessExecutionError(result)  # 把非零退出码作为失败，而不是成功文本
            return result.summary()  # 0 退出码转换为限长字符串，写入 BackgroundJob.result
        return execute  # 返回函数对象，不会立即执行命令

    def submit(self, manager: BackgroundJobManager, name: str, spec: ProcessSpec, task_id: str | None = None) -> str:  # 通过 V5.1 后台线程池运行子进程
        function = self.as_job_function(spec)  # 生成满足 JobFunction 签名的适配器
        return manager.submit(name=name, function=function, task_id=task_id)  # 立即返回持久化 Job ID
