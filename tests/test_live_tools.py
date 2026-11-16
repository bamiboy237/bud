"""Tools can be replaced between runs without rebuilding the agent."""

import asyncio
from typing import Any, Self

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.usage import RunUsage

from bud.composio.tools import compose_tools
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.setup import (
    SetupEntry,
    SetupInitiation,
    SetupJournal,
    SetupRegistry,
    SetupStatus,
    setup_tool,
)
from bud.gateway.registry import ModelSpec, Registry
from bud.harness.actions import Policy
from bud.harness.agent import Harness
from bud.harness.context import BackgroundCompaction
from bud.harness.live import LiveState, LiveTools


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


class Recording(AbstractToolset[None]):
    """A leaf toolset that counts the runs it was entered for."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.entries = 0
        self.exits = 0

    @property
    def id(self) -> str:
        return self.name

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        return {}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        raise AssertionError("a recording toolset has no tools")

    async def __aenter__(self) -> Self:
        self.entries += 1
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        self.exits += 1
        return None


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


async def test_a_swap_during_a_run_takes_effect_at_the_next_one():
    first, second = Recording("first"), Recording("second")
    live = LiveTools(state=LiveState(current=first))

    async with live:
        live.swap(second)
        # The run that started on `first` keeps it, and exits it.
        assert live.state.current is first
    assert (first.entries, first.exits) == (1, 1)
    assert (second.entries, second.exits) == (0, 0)

    async with live:
        assert live.state.current is second
    assert (second.entries, second.exits) == (1, 1)


async def test_overlapping_runs_exit_the_toolsets_they_entered():
    first, second = Recording("first"), Recording("second")
    live = LiveTools(state=LiveState(current=first))
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def first_run():
        async with live:
            entered.set()
            await finish.wait()

    task = asyncio.create_task(first_run())
    await entered.wait()
    live.swap(second)
    async with live:
        assert live.active.get() is second
    finish.set()
    await task
    assert (first.entries, first.exits) == (1, 1)
    assert (second.entries, second.exits) == (1, 1)


async def test_a_swap_mid_run_reaches_only_the_next_run(tmp_path):
    subject = harness(tmp_path)
    subject.policy.tools["late_tool"] = "allow"
    seen: list[set[str]] = []

    def respond(messages, info):
        seen.append({definition.name for definition in info.function_tools})
        if len(seen) == 1:
            subject.swap_tools(FunctionToolset[None]([tool("late_tool")]))
        return ModelResponse(parts=[TextPart("ok")])

    agent: Agent[None, str] = subject.agent(
        FunctionModel(respond), lambda _: None, context=BackgroundCompaction(record=lambda _: None)
    )
    await agent.run("first")
    assert "late_tool" not in seen[0]
    await agent.run("second")
    assert "late_tool" in seen[1]


async def test_a_discovered_app_reaches_the_agent_when_its_card_settles(tmp_path):
    subject = harness(tmp_path)
    subject.policy.tools["slack_send"] = "ask"
    journal = SetupJournal()
    journal.bind(Hub(EventLog(tmp_path / "events.db")))
    states = [SetupStatus(state="waiting")]
    registry = SetupRegistry()
    registry.register(
        "slack",
        SetupEntry(
            label="Slack",
            kind="url",
            initiate=lambda: SetupInitiation(kind="url", url="https://example.test/slack"),
            status=lambda: states[0],
        ),
    )

    def refresh() -> None:
        subject.swap_tools(FunctionToolset[None]([tool("slack_send")]))

    setup = setup_tool(registry, journal, refresh=refresh)

    def context(call_id: str) -> RunContext[None]:
        return RunContext(
            deps=None,
            model=TestModel(),
            usage=RunUsage(),
            run_id="run-1",
            tool_call_id=call_id,
            metadata={"bud_session_id": "session-1"},
        )

    assert setup(context("call-1"), "slack")["status"] == "waiting"
    states[0] = SetupStatus(state="connected")
    assert setup(context("call-2"), "slack")["status"] == "connected"

    seen: list[set[str]] = []

    def respond(messages, info):
        seen.append({definition.name for definition in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent: Agent[None, str] = subject.agent(
        FunctionModel(respond), lambda _: None, context=BackgroundCompaction(record=lambda _: None)
    )
    await agent.run("post the update")
    assert "slack_send" in seen[-1]
