from typing import Protocol
from devpilot.core.message import Message

class Model(Protocol):

    def complete(self,message:list[Message])->str :
        """Generate the next assistant response."""
        ...

class MockModel:

    def complete(self, messages: list[Message]) -> str:
        last_message = messages[-1]
        return f"Mock response to: {last_message.content}"