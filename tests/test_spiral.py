from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud.harness.spiral import (
    ProgressJudge,
    SpiralConfig,
    SpiralGuard,
    paused_in,
)


def now():
    return datetime.now(UTC)


def make_ctx(run_id="run-1"):
    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        run_id=run_id,
        pending_messages=[],
    )


@pytest.mark.asyncio
async def test_repeat_calls_triggers_system_prompt_nudge_without_hidden_note():
    reported = []
    config = SpiralConfig(repeat_note=2, repeat_pause=3)
    guard = SpiralGuard(config=config, report=reported.append)

    ctx = make_ctx()
    req1 = ModelRequest(parts=[UserPromptPart(content="inspect files")])

    # Step 1: Tool call
    resp1 = ModelResponse(
        parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c1")],
        timestamp=now(),
    )
    await guard.after_model_request(
        ctx,
        request_context=MagicMock(),
        response=resp1,
    )

    req2 = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="read_file", content="hello", tool_call_id="c1"),
        ]
    )
    req_ctx2 = MagicMock()
    req_ctx2.messages = [req1, resp1, req2]
    req_ctx2.model_request_parameters.instruction_parts = []
    req_ctx2.model_request_parameters.output_tools = []
    req_ctx2.model_request_parameters.allow_text_output = True
    req_ctx2.model_settings = {}

    await guard.before_model_request(ctx, req_ctx2)
    assert not guard._nudged

    # Step 2: Same tool call and same result
    resp2 = ModelResponse(
        parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c2")],
        timestamp=now(),
    )
    await guard.after_model_request(ctx, request_context=MagicMock(), response=resp2)

    req3 = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="read_file", content="hello", tool_call_id="c2"),
        ]
    )
    req_ctx3 = MagicMock()
    req_ctx3.messages = [req1, resp1, req2, resp2, req3]
    req_ctx3.model_request_parameters.instruction_parts = []
    req_ctx3.model_request_parameters.output_tools = []
    req_ctx3.model_request_parameters.allow_text_output = True
    req_ctx3.model_settings = {}

    await guard.before_model_request(ctx, req_ctx3)

    # First loop triggers nudge
    assert guard._nudged is True
    assert guard._paused is False
    assert len(reported) == 0

    # Verify nudge was enqueued as SystemPromptPart
    enqueued_parts = [
        part for pending in ctx.pending_messages for msg in pending.messages for part in msg.parts
    ]
    system_parts = [p for p in enqueued_parts if isinstance(p, SystemPromptPart)]
    assert len(system_parts) >= 1
    assert system_parts[0].metadata.get("phase") == "nudge"
    assert "read_file" in system_parts[0].content

    # Crucial invariant: ModelResponse was NOT modified with hidden notes
    for part in resp2.parts:
        if isinstance(part, TextPart):
            assert "Self-check" not in part.content


@pytest.mark.asyncio
async def test_second_loop_triggers_pause_and_loop_detected_report():
    reported = []
    config = SpiralConfig(repeat_note=2, repeat_pause=3)
    guard = SpiralGuard(config=config, report=reported.append)
    ctx = make_ctx()

    req1 = ModelRequest(parts=[UserPromptPart(content="inspect files")])

    # Call 1
    resp1 = ModelResponse(
        parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c1")],
        timestamp=now(),
    )
    await guard.after_model_request(ctx, request_context=MagicMock(), response=resp1)

    req2 = ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content="hello", tool_call_id="c1")]
    )
    req_ctx2 = MagicMock()
    req_ctx2.messages = [req1, resp1, req2]
    req_ctx2.model_request_parameters.instruction_parts = []
    req_ctx2.model_request_parameters.output_tools = []
    req_ctx2.model_request_parameters.allow_text_output = True
    req_ctx2.model_settings = {}
    await guard.before_model_request(ctx, req_ctx2)

    # Call 2 -> Nudge
    resp2 = ModelResponse(
        parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c2")],
        timestamp=now(),
    )
    await guard.after_model_request(ctx, request_context=MagicMock(), response=resp2)

    req3 = ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content="hello", tool_call_id="c2")]
    )
    req_ctx3 = MagicMock()
    req_ctx3.messages = [req1, resp1, req2, resp2, req3]
    req_ctx3.model_request_parameters.instruction_parts = []
    req_ctx3.model_request_parameters.output_tools = []
    req_ctx3.model_request_parameters.allow_text_output = True
    req_ctx3.model_settings = {}
    await guard.before_model_request(ctx, req_ctx3)
    assert guard._nudged is True
    assert guard._paused is False

    # Call 3 (streak reaches repeat_pause = 3) -> Pause!
    resp3 = ModelResponse(
        parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c3")],
        timestamp=now(),
    )
    await guard.after_model_request(ctx, request_context=MagicMock(), response=resp3)

    req4 = ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content="hello", tool_call_id="c3")]
    )
    req_ctx4 = MagicMock()
    req_ctx4.messages = [req1, resp1, req2, resp2, req3, resp3, req4]
    req_ctx4.model_request_parameters.instruction_parts = []
    req_ctx4.model_request_parameters.output_tools = []
    req_ctx4.model_request_parameters.allow_text_output = True
    req_ctx4.model_settings = {}

    res_ctx = await guard.before_model_request(ctx, req_ctx4)

    assert guard._paused is True
    assert len(reported) == 1
    assert reported[0]["run_id"] == "run-1"
    assert reported[0]["kind"] == "repeated_result"

    # Work tools must be turned off
    assert res_ctx.model_settings["tool_choice"] == "none"

    # Handoff response is recorded with pause metadata
    handoff_text = "Done: verified a.txt\nStuck on: loop\nNext: check b.txt"
    handoff_resp = ModelResponse(parts=[TextPart(handoff_text)])
    await guard.after_model_request(ctx, request_context=MagicMock(), response=handoff_resp)

    pause_record = paused_in([handoff_resp])
    assert pause_record is not None
    assert pause_record["phase"] == "pause"


@pytest.mark.asyncio
async def test_polling_with_changing_results_does_not_trigger_loop():
    reported = []
    # Configure high repeated_call_threshold so call count alone doesn't flag polling
    config = SpiralConfig(repeat_note=2, repeat_pause=3)
    guard = SpiralGuard(config=config, report=reported.append)
    guard.repeated_call_threshold = 10
    ctx = make_ctx()

    history = [ModelRequest(parts=[UserPromptPart(content="poll status")])]
    for i in range(1, 4):
        resp = ModelResponse(
            parts=[ToolCallPart(tool_name="check_status", args={"page": i}, tool_call_id=f"c{i}")],
            timestamp=now(),
        )
        await guard.after_model_request(ctx, request_context=MagicMock(), response=resp)

        req = ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="check_status",
                    content=f"status_progress_{i}",  # Result changes each time!
                    tool_call_id=f"c{i}",
                )
            ]
        )
        history.extend([resp, req])
        req_ctx = MagicMock()
        req_ctx.messages = list(history)
        req_ctx.model_request_parameters.instruction_parts = []
        req_ctx.model_request_parameters.output_tools = []
        req_ctx.model_request_parameters.allow_text_output = True
        req_ctx.model_settings = {}

        await guard.before_model_request(ctx, req_ctx)

    assert not guard._nudged
    assert not guard._paused
    assert len(reported) == 0


@pytest.mark.asyncio
async def test_loop_state_resets_on_continuation_run():
    config = SpiralConfig(repeat_note=2, repeat_pause=3)
    guard = SpiralGuard(config=config, report=lambda _: None)

    # Simulate run 1 detecting a nudge
    guard._nudged = True
    guard._requests_count = 5
    guard._steps.append(MagicMock())

    ctx = make_ctx(run_id="run-continuation")
    fresh_guard = await guard.for_run(ctx)

    assert fresh_guard._nudged is False
    assert fresh_guard._paused is False
    assert fresh_guard._requests_count == 0
    assert len(fresh_guard._steps) == 0


@pytest.mark.asyncio
async def test_progress_judge_triggers_when_probability_below_threshold():
    reported = []
    config = SpiralConfig(
        progress_first=1,
        progress_every=1,
        progress_threshold=0.2,
    )
    mock_judge = MagicMock(spec=ProgressJudge)
    mock_judge.progress_probability = AsyncMock(return_value=0.05)  # low progress

    guard = SpiralGuard(config=config, report=reported.append, judge=mock_judge)
    ctx = make_ctx()

    req = ModelRequest(parts=[UserPromptPart(content="solve tough problem")])
    req_ctx = MagicMock()
    req_ctx.messages = [req]
    req_ctx.model_request_parameters.instruction_parts = []
    req_ctx.model_request_parameters.output_tools = []
    req_ctx.model_request_parameters.allow_text_output = True
    req_ctx.model_settings = {}

    # Turn 1: Progress check triggers nudge
    await guard.before_model_request(ctx, req_ctx)
    assert guard._nudged is True
    assert guard._paused is False
    assert len(reported) == 0

    enqueued_parts = [
        part for pending in ctx.pending_messages for msg in pending.messages for part in msg.parts
    ]
    system_parts = [p for p in enqueued_parts if isinstance(p, SystemPromptPart)]
    assert len(system_parts) >= 1
    assert system_parts[0].metadata.get("pattern") == "no_progress"

    # Turn 2: Second check triggers pause
    req_ctx2 = MagicMock()
    req_ctx2.messages = [req]
    req_ctx2.model_request_parameters.instruction_parts = []
    req_ctx2.model_request_parameters.output_tools = []
    req_ctx2.model_request_parameters.allow_text_output = True
    req_ctx2.model_settings = {}

    res_ctx = await guard.before_model_request(ctx, req_ctx2)
    assert guard._paused is True
    assert len(reported) == 1
    assert reported[0]["kind"] == "no_progress"
    assert res_ctx.model_settings["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_alternating_calls_cycle_detection():
    reported = []
    config = SpiralConfig(cycle_repeats=2)
    guard = SpiralGuard(config=config, report=reported.append)
    ctx = make_ctx()

    history = [ModelRequest(parts=[UserPromptPart(content="alternating cycle")])]
    # 2 cycles of toolA, toolB = 4 calls
    calls = [("toolA", "c1"), ("toolB", "c2"), ("toolA", "c3"), ("toolB", "c4")]
    for tool_name, call_id in calls:
        resp = ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={"arg": 1}, tool_call_id=call_id)],
            timestamp=now(),
        )
        await guard.after_model_request(ctx, request_context=MagicMock(), response=resp)

        req = ModelRequest(
            parts=[ToolReturnPart(tool_name=tool_name, content="res", tool_call_id=call_id)]
        )
        history.extend([resp, req])
        req_ctx = MagicMock()
        req_ctx.messages = list(history)
        req_ctx.model_request_parameters.instruction_parts = []
        req_ctx.model_request_parameters.output_tools = []
        req_ctx.model_request_parameters.allow_text_output = True
        req_ctx.model_settings = {}

        await guard.before_model_request(ctx, req_ctx)

    assert guard._nudged is True
    assert guard._paused is False
    enqueued_parts = [
        part for pending in ctx.pending_messages for msg in pending.messages for part in msg.parts
    ]
    system_parts = [p for p in enqueued_parts if isinstance(p, SystemPromptPart)]
    assert len(system_parts) >= 1
    assert system_parts[0].metadata.get("pattern") == "alternating_calls"


@pytest.mark.asyncio
async def test_repeated_text_chanting_detection():
    reported = []
    config = SpiralConfig(chant_repeats=2)
    guard = SpiralGuard(config=config, report=reported.append)
    ctx = make_ctx()

    # 2 consecutive text responses with same text
    for _ in range(2):
        resp = ModelResponse(
            parts=[TextPart("I am waiting for your response.")],
            timestamp=now(),
        )
        await guard.after_model_request(ctx, request_context=MagicMock(), response=resp)

        req_ctx = MagicMock()
        req_ctx.messages = [resp]
        req_ctx.model_request_parameters.instruction_parts = []
        req_ctx.model_request_parameters.output_tools = []
        req_ctx.model_request_parameters.allow_text_output = True
        req_ctx.model_settings = {}

        await guard.before_model_request(ctx, req_ctx)

    assert guard._nudged is True
    assert guard._paused is False
    enqueued_parts = [
        part for pending in ctx.pending_messages for msg in pending.messages for part in msg.parts
    ]
    system_parts = [p for p in enqueued_parts if isinstance(p, SystemPromptPart)]
    assert len(system_parts) >= 1
    assert system_parts[0].metadata.get("pattern") == "repeated_text"
