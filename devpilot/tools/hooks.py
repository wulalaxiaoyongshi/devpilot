from collections.abc import Callable
from devpilot.core.message import ToolCall

class HookManager:

    def __init__(self):
        self._pre_tool_use:list[Callable[[ToolCall],str|None]]=[]
        self._post_tool_use: list[Callable[[ToolCall,str],None]] = []

    def register_pre_tool_use(self,hook: Callable[[ToolCall],str | None,],) -> None:
        self._pre_tool_use.append(hook)

    def register_post_tool_use(self,hook: Callable[[ToolCall, str],None,],) -> None:
        self._post_tool_use.append(hook)

    def run_pre_tool_use(self,tool_call: ToolCall,) -> str | None:
        for hook in self._pre_tool_use:
            result = hook(tool_call)
            if result is not None:
                return result
        return None

    def run_post_tool_use(self,tool_call: ToolCall,result: str,) -> None:
        for hook in self._post_tool_use:
            hook(tool_call,result,)