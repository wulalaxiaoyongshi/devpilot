from devpilot.core.agent import Agent
from devpilot.core.model import DeepSeekModel

def test_agent_returns_model_response():

    agent = Agent(model=DeepSeekModel())

    result = agent.run(
        "你好，请用一句话告诉我你是谁。"
    )

    print(response)

