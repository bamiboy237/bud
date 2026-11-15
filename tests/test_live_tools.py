"""Tools can be replaced between runs without rebuilding the agent."""

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.tools import compose_tools
from bud.gateway.registry import ModelSpec, Registry
from bud.harness.actions import Policy
from bud.harness.agent import Harness
from bud.harness.context import BackgroundCompaction


def harness(tmp_path):
    registry = Registry(
        models=[
            ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
        ]
    )
    return Harness(
        FunctionToolset[None]([]),
        Policy(tools={}),
        registry,
        "A synthetic profile for tests.",
        state_dir=tmp_path,
        result_path=tmp_path / "results.db",
        read_tools=set(),
    )


def tool(name: str):
    def run() -> str:
        return f"{name} ran"

    run.__name__ = name
    return run


async def test_app_tools_are_deferred_and_bud_tools_stay_visible():
    seen: list[list[str]] = []

    def respond(messages, info):
        seen.append(sorted(definition.name for definition in info.function_tools))
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        FunctionModel(respond),
        toolsets=[
            compose_tools(
                FunctionToolset[None]([tool("slack_send")]),
                [Tool(tool("ask_user"))],
            )
        ],
    )
    await agent.run("hi")
    # The app's tool waits behind tool search; bud's own tool is always offered.
    assert "ask_user" in seen[-1]
    assert "slack_send" not in seen[-1]


async def test_agent_sees_a_tool_added_after_it_was_built(tmp_path):
    subject = harness(tmp_path)
    seen: list[set[str]] = []

    def respond(messages, info):
        seen.append({definition.name for definition in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent: Agent[None, str] = subject.agent(
        FunctionModel(respond), lambda _: None, context=BackgroundCompaction(record=lambda _: None)
    )
    await agent.run("first")
    assert "added_later" not in seen[-1]

    subject.swap_tools(FunctionToolset[None]([tool("added_later")]))
    await agent.run("second")
    # A swapped-in tool nobody classified stays hidden: the guard denies it.
    assert "added_later" not in seen[-1]

    subject.policy.tools["added_later"] = "allow"
    await agent.run("third")
    assert "added_later" in seen[-1]
    # The first run kept the tools it started with.
    assert "added_later" not in seen[0]
