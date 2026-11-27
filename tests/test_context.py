import re
from datetime import timedelta

import pytest
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import (
    CompactionPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage
from pydantic_ai_harness.compaction import estimate_token_count

from bud.core.log import EventLog
from bud.gateway.errors import WorkingContextBudgetError
from bud.harness.context import BackgroundCompaction
from bud.harness.results import SqliteResultStore


def history(count, size):
    messages = [ModelRequest(parts=[UserPromptPart("Summarize my inbox")])]
    for index in range(count):
        identity = f"call-{index}"
        messages.extend(
            [
                ModelResponse(parts=[ToolCallPart("search_email", {}, tool_call_id=identity)]),
                ModelRequest(
                    parts=[ToolReturnPart("search_email", "x " * size, tool_call_id=identity)]
                ),
            ]
        )
    messages.extend(
        [
            ModelResponse(parts=[ToolCallPart("ask_user", {}, tool_call_id="owner")]),
            ModelRequest(parts=[ToolReturnPart("ask_user", "Only Sam Lee", tool_call_id="owner")]),
        ]
    )
    return messages


async def compact(messages):
    calls = []

    async def respond(messages, info):
        calls.append(True)
        yield "The owner wants an inbox summary."

    model = FunctionModel(stream_function=respond)
    records = []
    compaction = BackgroundCompaction(record=records.append)
    ctx = RunContext(
        deps=None,
        model=model,
        usage=RunUsage(),
        messages=messages,
        run_id="run",
        conversation_id="session",
    )
    request = ModelRequestContext(
        model=model,
        messages=messages,
        model_settings=None,
        model_request_parameters=ModelRequestParameters(),
    )
    result = await compaction._summarize(ctx, request)
    return result, calls, records


async def test_old_tool_results_are_cleared_without_a_model_call():
    messages = history(16, 6000)
    before = estimate_token_count(messages)
    result, calls, records = await compact(messages)
    assert before > 41_600
    assert estimate_token_count(result) < 29_120
    assert not calls
    assert records[-1]["usage"]["requests"] == 0
    returns = [
        part for message in result for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    assert any(part.tool_name == "ask_user" and part.content == "Only Sam Lee" for part in returns)
    assert estimate_token_count(messages) == before


async def test_small_history_is_unchanged_without_a_model_call():
    messages = history(2, 20)
    result, calls, _ = await compact(messages)
    assert result == messages
    assert not calls


@pytest.mark.parametrize(
    ("request_window", "result_size", "stage"),
    [(64_000, 38_000, "compacted"), (64_000, 23_000, "preparing"), (200_000, 38_000, None)],
)
async def test_compaction_uses_the_request_model_window(request_window, result_size, stage):
    messages = history(3, result_size)
    records = []
    model_calls = []

    async def summarize(messages, info):
        model_calls.append(True)
        yield "An inbox summary."

    ctx_model = FunctionModel(profile={"context_window": 1_000_000}, stream_function=summarize)
    request_model = FunctionModel(
        profile={"context_window": request_window}, stream_function=summarize
    )
    ctx = RunContext(
        deps=None,
        model=ctx_model,
        usage=RunUsage(),
        messages=messages,
        run_id="run",
        conversation_id="session",
    )
    request = ModelRequestContext(
        model=request_model,
        messages=messages,
        model_settings=None,
        model_request_parameters=ModelRequestParameters(),
    )
    compaction = BackgroundCompaction(record=records.append)
    try:
        result = await compaction.before_model_request(ctx, request)
        assert (records[-1]["stage"] if records else None) == stage
    finally:
        await compaction.close()
    if stage == "compacted":
        assert model_calls == [True]
        assert estimate_token_count(result.messages) < 41_600
    else:
        assert result.messages == messages


@pytest.mark.parametrize("request_window", [200_000, 64])
async def test_no_cutoff_checks_the_current_request_budget(request_window):
    messages = [
        ModelRequest(parts=[UserPromptPart("Summarize my inbox")]),
        ModelResponse(
            parts=[TextPart("Inbox summary")],
            model_name="deepseek-flash",
            usage=RequestUsage(input_tokens=650_000, output_tokens=5),
        ),
    ]
    received = []
    records = []

    def respond(messages, info):
        received.extend(messages)
        return ModelResponse(parts=[TextPart("Updated summary")])

    agent = Agent(
        FunctionModel(respond, profile={"context_window": request_window}),
        capabilities=[BackgroundCompaction(record=records.append)],
    )
    run = agent.run("Continue", message_history=messages, conversation_id="session", run_id="run")
    if request_window == 64:
        with pytest.raises(WorkingContextBudgetError):
            await run
        assert not received
    else:
        assert (await run).output == "Updated summary"
        assert received[:2] == messages
    assert all(record["stage"] != "compacted" for record in records)


async def test_old_app_results_stay_available_without_refilling_the_prompt(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "events.db")
    log.append_once("session", "run.queued", {}, key="old-run:queued")
    log.append_once("session", "run.queued", {}, key="new-run:queued")
    store = SqliteResultStore(log.path, retention=timedelta(hours=1))
    payload = "A repository row.\n" * 12_000
    messages = [
        ModelRequest(parts=[UserPromptPart("List the repositories")]),
        ModelResponse(parts=[ToolCallPart("GITHUB_LIST_REPOSITORIES", {}, tool_call_id="repos")]),
        ModelRequest(
            parts=[ToolReturnPart("GITHUB_LIST_REPOSITORIES", payload, tool_call_id="repos")],
            run_id="old-run",
        ),
        ModelResponse(parts=[TextPart("I listed the repositories.")]),
    ]
    received = []

    def respond(messages, _):
        received.extend(p for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        return ModelResponse(parts=[TextPart("Hello.")])

    agent = Agent(
        FunctionModel(respond),
        capabilities=[BackgroundCompaction(record=lambda _: None, result_store=store)],
    )
    await agent.run("Hi", message_history=messages, conversation_id="session", run_id="new-run")
    [result] = received
    assert len(result.content) < 300
    assert "Earlier GITHUB_LIST_REPOSITORIES result" in result.content
    # Named after the run that produced it, so every later turn sends the same text.
    handle = re.search(r"old-run/history-[0-9a-f]{64}", result.content)
    assert handle is not None
    assert await store.load_tool_result(handle[0]) == payload
    original = messages[2].parts[0]
    assert isinstance(original, ToolReturnPart)
    assert original.content == payload

    # A later step finds the stored copy and does not lock the database to write it again.
    writes = []
    monkeypatch.setattr(store, "write", lambda *args: writes.append(args))
    await agent.run("Again", message_history=messages, conversation_id="session", run_id="new-run")
    assert received[-1].content == result.content
    assert writes == []


async def test_compaction_summarizes_when_recent_results_cannot_be_cleared():
    messages = history(3, 20000)
    result, calls, records = await compact(messages)
    assert len(calls) == 1
    assert estimate_token_count(result) < estimate_token_count(messages)
    assert records[-1]["usage"]["requests"] == 1
    assert any(
        part.tool_name == "ask_user" and part.content == "Only Sam Lee"
        for message in result
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )


@pytest.mark.parametrize("defer_loading", [True, False])
async def test_compacted_history_budget_counts_only_tools_sent_to_model(defer_loading):
    def lookup(query: str) -> str:
        return query

    def respond(messages, info):
        assert "lookup" not in {tool.name for tool in info.function_tools}
        assert any(
            isinstance(part, CompactionPart) for message in messages for part in message.parts
        )
        return ModelResponse(parts=[TextPart("Here is the inbox summary.")])

    async def summarize(messages, info):
        yield "The owner wants an inbox summary."

    records = []
    agent = Agent(
        FunctionModel(respond, stream_function=summarize),
        capabilities=[
            BackgroundCompaction(record=records.append),
            Capability(
                id="large_catalog",
                tools=[Tool(lookup, description="Detailed schema guidance. " * 6_000)],
                defer_loading=defer_loading,
            ),
        ],
    )
    run = agent.run(
        "Continue the summary",
        message_history=history(3, 40_000),
        conversation_id="session",
        run_id="run",
    )
    if defer_loading:
        assert (await run).output == "Here is the inbox summary."
        assert [record["stage"] for record in records] == ["ready", "compacted"]
    else:
        with pytest.raises(WorkingContextBudgetError, match="exceed bud's working context budget"):
            await run
