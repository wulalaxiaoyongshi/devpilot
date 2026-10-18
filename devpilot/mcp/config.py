"""定义由宿主程序指定的本地 MCP Server 启动配置。"""  # 本文件只管理配置，不连接服务器

import sys  # 获取运行当前程序的 Python 解释器
from dataclasses import dataclass  # 使用数据类保存不可变配置
from pathlib import Path  # 生成跨平台的绝对文件路径


@dataclass(frozen=True)  # 创建不可修改的配置对象
class LocalServerConfig:  # 描述如何启动本地 MCP Server
    command: str  # 可执行程序路径，必须由可信宿主配置
    args: tuple[str, ...] = ()  # 传给程序的参数列表，不拼接为 Shell 命令
    env: dict[str, str] | None = None  # 可选的子进程环境变量

    def __post_init__(self) -> None:  # 对配置做基础校验
        if not self.command.strip():  # 启动命令不能为空
            raise ValueError("MCP Server command cannot be empty")  # 阻止无效启动命令
        if any(not isinstance(item, str) for item in self.args):  # 每项参数都必须是字符串
            raise TypeError("All MCP Server arguments must be strings")  # 阻止不合法参数

    @classmethod  # 提供开箱即用的 Demo 配置构造器
    def demo(cls) -> "LocalServerConfig":  # 用当前 Python 解释器启动随包提供的 Demo Server
        script = Path(__file__).resolve().parent / "servers" / "demo_server.py"  # 找到 Demo Server 的绝对路径
        return cls(command=sys.executable, args=("-u", str(script)))  # 不用 Shell，支持带空格的 Windows 路径
