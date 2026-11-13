"""V9.8：证明 V2、V3、V4 的非核心能力在需要时确实可调用。"""

from __future__ import annotations

from devpilot.agents.mailbox import MailboxStore, create_mailbox_tools
from devpilot.context.compact import ContextCompactor, ContextConfig
from devpilot.context.skills import SkillCatalog, create_load_skill_tool
from devpilot.context.todo import TodoManager, create_todo_tool
from devpilot.context.transcript import TranscriptStore
from devpilot.core.message import Message
from devpilot.core.model import ModelResponse
from devpilot.memory.manager import MemoryManager
from devpilot.memory.store import MemoryStore


class NoNetworkModel:
    """禁用任何真实网络请求，仅服务 V2/3 本地能力的离线测试。"""

    def complete(self, messages, tools=None, system=None):
        """在需要历史摘要时返回固定摘要，证明压缩路径已触发。"""
        return ModelResponse(content="Summarized test history")


def test_v2_large_tool_output_offloaded_without_losing_original(tmp_path):
    """V2 真正把过大的工具结果写到磁盘，并向模型提供截断预览。"""
    compactor = ContextCompactor(NoNetworkModel(), tmp_path, TranscriptStore(tmp_path / "transcript"),
                                 ContextConfig(context_char_limit=10000, large_tool_result_limit=35, tool_preview_chars=15))
    original = "long_" * 120
    messages = [Message(role="user", content="Analyze logs"),
                Message(role="tool", content=original, tool_call_id="tool-123")]
    prepared = compactor.prepare(messages)
    assert "Large tool output stored" in prepared[1].content
    assert (tmp_path / ".devpilot" / "tool_outputs" / "tool-123.txt").read_text(encoding="utf-8") == original
    assert messages[1].content == original


def test_v2_todo_tool_and_skill_on_demand(tmp_path):
    """V2 Todo 与 load_skill 通过真实工具 handler 操作各自数据。"""
    skills = tmp_path / "skills" / "debugging"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Debugging\nAlways check failing tests.\n", encoding="utf-8")
    catalog = SkillCatalog(tmp_path / "skills")
    assert "debugging" in catalog.catalog_text()
    output = create_load_skill_tool(catalog).handler({"name": "debugging"})
    assert "Always check failing tests" in output
    todos = TodoManager()
    assert "inspect" in create_todo_tool(todos).handler({"items": [
        {"content": "inspect", "status": "completed"}]})


def test_v3_memory_persisted_and_recalled(tmp_path):
    """V3 记忆能够跨 Manager 实例持久保存，并由 memory_search 工具按关键词检索。"""
    path = tmp_path / "shared_memory.json"
    writer = MemoryManager(MemoryStore(path), NoNetworkModel(), auto_extract=False)
    assert "Memory saved" in writer.remember("Checkout uses pytest as its test runner", "project", ["pytest"])
    reader = MemoryManager(MemoryStore(path), NoNetworkModel(), auto_extract=False)
    search = next(tool for tool in reader.create_tools() if tool.name == "memory_search")
    assert "Checkout uses pytest" in search.handler({"query": "pytest"})


def test_v4_mailbox_between_worker_identities(tmp_path):
    """V4 Mailbox 支持真实的 Worker 发消息、另一个 Worker 读取并持久化已读状态。"""
    path = tmp_path / "mailbox.json"
    mailbox = MailboxStore(path)
    sent = mailbox.send("worker_a", "worker_b", "Please re-run auth tests")
    received = MailboxStore(path).read("worker_b")
    assert len(received) == 1 and received[0].id == sent.id
    assert MailboxStore(path).read("worker_b", unread_only=True) == []
