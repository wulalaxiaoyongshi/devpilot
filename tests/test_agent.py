from devpilot.core.agent import Agent
from devpilot.core.model import MockModel

def test_agent_returns_model_response():

    agent = Agent(
        model=MockModel()
    )

    result = agent.run(
        "hello"
    )

    assert result == "Mock response to: hello"


def test_agent_keeps_message_history():

    agent = Agent(
        model=MockModel()
    )

    agent.run("first")
    agent.run("second")

    assert len(agent.messages) == 4

    assert agent.messages[0].role == "user"
    assert agent.messages[0].content == "first"

    assert agent.messages[2].role == "user"
    assert agent.messages[2].content == "second"