"""Spiral detection through a real agent run: nudge once, then pause with tools off."""

import json
from itertools import count

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bud.harness.spiral import (
    NUDGE,
    SpiralConfig,
    SpiralGuard,
    loop_detector,
    marks,
    paused_in,
)
from tests.runner_support import reply, running_bud

HANDOFF_TEXT = "Done: nothing. Stuck on: the report. Next step: ask the owner."


def scripted(
    results: list[str],
    config: SpiralConfig,
    reports: list[dict],
    *,
    judge=None,
    until: int | None = None,
) -> tuple[Agent[None, str], list[int]]:
    """An agent that calls `check` until its tools are off, or `until` calls have run."""
    calls: list[int] = []
    ids = count()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if (info.model_settings or {}).get("tool_choice") == "none":
            return ModelResponse(parts=[TextPart(HANDOFF_TEXT)])
        if until is not None and len(calls) >= until:
            return ModelResponse(parts=[TextPart("ready")])
        return ModelResponse(parts=[ToolCallPart("check", {}, tool_call_id=f"c{next(ids)}")])

    agent = Agent(
        FunctionModel(model),
        output_type=str,
        capabilities=[
            loop_detector(config, reports.append),
            SpiralGuard(config=config, report=reports.append, judge=judge),
        ],
    )

    @agent.tool_plain
    def check() -> str:
        calls.append(len(calls))
        return results[min(len(calls) - 1, len(results) - 1)]

    return agent, calls


def nudges(messages: list[ModelMessage]) -> list[dict]:
    return [mark for mark in marks(messages) if mark["origin"] == NUDGE]


async def test_a_repeated_result_is_nudged_then_paused_with_a_handoff():
    reports: list[dict] = []
    agent, calls = scripted(["pending"], SpiralConfig(repeat_note=2), reports)

    result = await agent.run("Get me the report.")

    assert result.output == HANDOFF_TEXT
    # Two identical results raise the nudge; two more after it pause the run.
    assert len(calls) == 4
    assert [n["kind"] for n in nudges(result.all_messages())] == ["repeated_result"]
    pause = paused_in(result.all_messages())
    assert pause is not None and pause["kind"] == "repeated_result"
    assert [r["kind"] for r in reports] == ["repeated_result"]
    assert reports[0]["run_id"] == result.run_id


async def test_polling_with_changing_results_is_not_a_loop():
    reports: list[dict] = []
    pending = [f"pending {i}" for i in range(10)]
    agent, calls = scripted(pending, SpiralConfig(repeat_note=2), reports, until=7)

    result = await agent.run("Wait for the report.")

    assert result.output == "ready"
    assert len(calls) == 7
    assert reports == []
    assert nudges(result.all_messages()) == []


class NoProgress:
    """A judge that always answers that the run is stuck."""

    def __init__(self) -> None:
        self.asked = 0

    async def progress_probability(self, request: str, log: str, total_steps: int) -> float:
        self.asked += 1
        return 0.0


async def test_the_progress_judge_climbs_the_same_ladder():
    # Results always change, so only the judge can see the run is stuck.
    reports: list[dict] = []
    judge = NoProgress()
    config = SpiralConfig(progress_first=2, progress_every=2)
    pages = [f"page {i}" for i in range(50)]
    agent, calls = scripted(pages, config, reports, judge=judge)

    result = await agent.run("Find the invoice.")

    assert result.output == HANDOFF_TEXT
    assert len(calls) == 4
    assert judge.asked == 2
    assert [n["kind"] for n in nudges(result.all_messages())] == ["no_progress"]
    assert [r["kind"] for r in reports] == ["no_progress"]
    assert reports[0]["confidence"] == 1.0


async def test_a_new_run_starts_the_ladder_again():
    reports: list[dict] = []
    agent, _ = scripted(["pending"], SpiralConfig(repeat_note=2), reports)

    first = await agent.run("Get me the report.")
    second = await agent.run("Try once more.", message_history=first.all_messages())

    new = second.new_messages()
    assert [n["kind"] for n in nudges(new)] == ["repeated_result"]
    assert paused_in(new) is not None
    assert len(reports) == 2


async def test_the_durable_runner_reports_the_pause_and_keeps_the_handoff(tmp_path):
    ids = count()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if (info.model_settings or {}).get("tool_choice") == "none":
            return reply(HANDOFF_TEXT)
        call = ToolCallPart("list_skills", {}, tool_call_id=f"l{next(ids)}")
        return ModelResponse(parts=[call])

    async with running_bud(tmp_path, model, spiral=SpiralConfig(repeat_note=2)) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "List my notes until one appears.", "message-1")
        await bud.settle()
        events = bud.log.read("owner")

    loops = [e.data for e in events if e.type == "run.loop_detected"]
    assert [loop["kind"] for loop in loops] == ["repeated_result"]
    completed = [e for e in events if e.type == "run.completed"]
    assert len(completed) == 1
    assert HANDOFF_TEXT in json.dumps(completed[0].data["messages"])
