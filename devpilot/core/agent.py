from devpilot.core.message import Message
from devpilot.core.model import  Model

class Agent:
    def __init__(self,model:Model):
        self.model=model
        self.messages:list[Message] = []

    def run(self,user_input : str) -> str:
        user_message=Message(role="user",content=user_input)
        self.messages.append(user_message)

        response=self.model.complete(self.messages)

        assistant_message=Message(role="assistant",content=response)
        self.messages.append(assistant_message)

        return response
