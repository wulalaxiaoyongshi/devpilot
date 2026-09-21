from devpilot.core.message import ToolCall
from devpilot.tools.hooks import HookManager
from devpilot.tools.registry import ToolRegistry

class ToolDispatcher:

    def __init__(self,registry: ToolRegistry,hooks: HookManager,):
        self.registry = registry
        self.hooks = hooks

    def execute(self,tool_call: ToolCall,) -> str:
        tool = self.registry.get(tool_call.name)
        if tool is None:
            return (f"Error: unknown tool "f"'{tool_call.name}'")
        # ------------------
        # PreToolUse
        # ------------------
        blocked = (self.hooks.run_pre_tool_use(tool_call))
        if blocked is not None:
            return blocked
        # ------------------
        # Execute
        # ------------------
        try:
            result = tool.handler(tool_call.arguments)
        except Exception as error:
            result = (
                f"{type(error).__name__}: "
                f"{error}"
            )
        # ------------------
        # PostToolUse
        # ------------------
        self.hooks.run_post_tool_use( tool_call,result,)
        return result