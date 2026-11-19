import re
from datetime import timedelta

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import estimate_token_count

from bud.core.log import EventLog
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
