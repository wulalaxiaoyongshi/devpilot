import json  # JSON 序列化与反序列化
from dataclasses import asdict, replace  # 数据类转字典、生成新状态对象
from pathlib import Path  # 处理磁盘路径
from threading import RLock  # 保证同一个 Store 实例的线程安全

from devpilot.background.model import BackgroundJob, utc_now  # 引用统一 Job 数据类型与时间函数


class JobStore:  # 管理后台任务状态的持久化
    def __init__(self, path: Path):  # 指定 JSON 数据文件
        self.path = path.resolve()  # 转成绝对路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 创建目录
        self._lock = RLock()  # 所有读写共用一把可重入锁
        with self._lock:  # 防止同一个实例并发初始化
            if not self.path.exists():  # 首次运行时才初始化文件
                self._write_unlocked([])  # 保存空任务列表

    def all(self) -> list[BackgroundJob]:  # 返回全部任务的快照
        with self._lock:  # 防止读取时本实例正在写文件
            return self._read_unlocked()  # 反序列化任务数据

    def get(self, job_id: str) -> BackgroundJob | None:  # 根据 ID 查询
        with self._lock:  # 读取同样受锁保护
            return next((job for job in self._read_unlocked() if job.id == job_id), None)  # 找不到返回 None

    def add(self, job: BackgroundJob) -> None:  # 写入新任务
        with self._lock:  # 将读取、校验和写入合成原子临界区
            jobs = self._read_unlocked()  # 获取最新状态
            if any(existing.id == job.id for existing in jobs):  # 检查重复 ID
                raise ValueError(f"Duplicate job ID: {job.id}")  # 不允许覆盖现有任务
            jobs.append(job)  # 在列表中增加记录
            self._write_unlocked(jobs)  # 持久化整个列表

    def update(self, job_id: str, **changes) -> BackgroundJob:  # 原子修改某一任务
        with self._lock:  # 避免两个线程覆盖彼此的更新
            jobs = self._read_unlocked()  # 读取最新版本
            for index, job in enumerate(jobs):  # 找到待更新任务
                if job.id == job_id:  # 匹配 ID
                    updated = replace(job, **changes)  # 创建新快照，不直接修改旧对象
                    jobs[index] = updated  # 把新快照写回列表
                    self._write_unlocked(jobs)  # 保存状态
                    return updated  # 将最新快照交给调用方
            raise KeyError(f"Unknown job: {job_id}")  # 找不到就报错

    def recover_interrupted(self) -> None:  # 进程重启时处理上次未结束的任务
        with self._lock:  # 恢复过程同样需要锁
            jobs = self._read_unlocked()  # 读取旧状态
            changed = False  # 记录有没有实际修改
            for index, job in enumerate(jobs):  # 逐一检查
                if job.status in {"pending", "running", "cancelling"}:  # 这些状态不可能在新进程里继续绑定旧 Future
                    jobs[index] = replace(job, status="interrupted", error="Previous runtime stopped before this job finished", finished_at=utc_now())  # 不假装自动恢复执行
                    changed = True  # 标记需要落盘
            if changed:  # 有状态变化才重写文件
                self._write_unlocked(jobs)  # 保存恢复结果

    def _read_unlocked(self) -> list[BackgroundJob]:  # 内部函数：调用前必须已持锁
        data = json.loads(self.path.read_text(encoding="utf-8"))  # 读取 JSON 文件
        if not isinstance(data, list):  # 防止意外文件结构
            raise ValueError("Job store must contain a JSON array")  # 给出明确错误
        return [BackgroundJob(**item) for item in data]  # 字典转换回 Job 数据类

    def _write_unlocked(self, jobs: list[BackgroundJob]) -> None:  # 内部函数：调用前必须已持锁
        temp = self.path.with_name(self.path.name + ".tmp")  # 临时文件避免直接覆盖原文件
        temp.write_text(json.dumps([asdict(job) for job in jobs], ensure_ascii=False, indent=2), encoding="utf-8")  # 序列化并写入临时文件
        temp.replace(self.path)  # 用原子替换操作更新正式 JSON 文件
