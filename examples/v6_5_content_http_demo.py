"""V6.5：用真实 HTTP MCP Server 体验资源、提示模板和现有 V6.2 工具调用。"""

import socket
import subprocess
import sys
import time
from pathlib import Path

from devpilot.mcp.content_client import RemoteServerConfig
from devpilot.mcp.content_runtime import MCPContentRuntime
from devpilot.mcp.sync_bridge import MCPBridge


def choose_port() -> int:
    """在本地寻找可用 TCP 端口，减少演示程序与其他服务端口冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main() -> None:
    """启动 HTTP Server，读取资源和提示模板后关闭独立子进程。"""
    server_file = Path(__file__).resolve().parents[1] / "devpilot" / "mcp" / "servers" / "content_http_server.py"
    port = choose_port()
    process = subprocess.Popen([sys.executable, str(server_file), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge = None
    try:
        endpoint = RemoteServerConfig(f"http://127.0.0.1:{port}/mcp")
        for attempt in range(30):
            try:
                bridge = MCPBridge.from_http(endpoint, timeout=2.0).start()
                break
            except (OSError, TimeoutError, RuntimeError):
                if process.poll() is not None:
                    raise RuntimeError("MCP HTTP Server exited before it became ready")
                time.sleep(0.2)
        if bridge is None:
            raise TimeoutError("MCP HTTP Server did not become ready")
        content = MCPContentRuntime(bridge, allowed_uri_prefixes=("devpilot://docs/",), allowed_prompts=frozenset({"review_request"}))
        print("Resources:", [x.uri for x in content.list_resources()])
        print("Resource templates:", [x.uri_template for x in content.list_resource_templates()])
        print("Guide:", content.read_resource("devpilot://docs/guide").text)
        print("Prompt names:", [x.name for x in content.list_prompts()])
        print("Prompt:", content.get_prompt("review_request", {"filename": "main.py"}).messages)
        print("MCP Tool:", bridge.call_tool("ping", {}).text)
    finally:
        if bridge is not None:
            bridge.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
