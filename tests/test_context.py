from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import estimate_token_count

from bud.harness.context import BackgroundCompaction


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
