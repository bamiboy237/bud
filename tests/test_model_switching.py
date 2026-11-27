from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RequestUsage

from bud.gateway import chatgpt
from bud.gateway.errors import WorkingContextBudgetError
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, load_registry
from bud.gateway.tracked import TrackedModel
from bud.harness.context import BackgroundCompaction


async def summarize_history(messages, info):
    yield "Earlier inbox task."


def terra_model(ledger, respond, *, window=200_000, summarize=summarize_history, output_limit=None):
    name = "gpt-6.1-terra"
    spec = ModelSpec(
        provider="openai-chatgpt",
        name=name,
        tier="frontier",
        billing="subscription",
        context_window=window,
        max_output_tokens=output_limit,
    )
    return WrapperModel(
        TrackedModel(
            FunctionModel(
                respond,
                stream_function=summarize,
                model_name=name,
                profile={"context_window": window},
            ),
            spec,
            ledger,
        )
    )


@pytest.mark.parametrize(
    ("previous_model", "should_compact"),
    [
        ("deepseek:deepseek-flash", False),
        ("openai:gpt-6.1-terra", False),
        ("OPENAI-CHATGPT:GPT-6.1-TERRA", True),
    ],
)
async def test_only_matching_provider_and_model_supply_token_anchor(
    tmp_path, previous_model, should_compact
):
    received = []
    summaries = []

    def respond(messages, info):
        received.extend(messages)
        return ModelResponse(parts=[TextPart("Done.")])

    async def summarize(messages, info):
        summaries.append(True)
        yield "Earlier inbox task."

    # This window makes 80K exceed the trigger while the actual request fits.
    model = terra_model(
        Ledger(tmp_path / "ledger.db"), respond, window=100_000, summarize=summarize
    )
    records = []
    compaction = BackgroundCompaction(record=records.append, keep_tokens=5)
    agent = Agent(model, capabilities=[compaction])
    history = [
        ModelRequest(parts=[UserPromptPart("Earlier inbox request " * 20)]),
        ModelResponse(
            parts=[TextPart("Reviewed.")],
            usage=RequestUsage(input_tokens=80_000, output_tokens=2),
            metadata={"bud_model_id": previous_model},
            model_name=previous_model.partition(":")[2],
            provider_name=previous_model.partition(":")[0],
        ),
    ]

    result = await agent.run(
        "Continue the inbox review",
        message_history=history,
        conversation_id="session",
        run_id="run",
    )

    assert result.output == "Done."
    assert summaries == ([True] if should_compact else [])
    assert [record["stage"] for record in records] == (
        ["ready", "compacted"] if should_compact else []
    )
    if not should_compact:
        assert received[:2] == history


@pytest.mark.parametrize("body_tokens", [5_600, 7_000])
async def test_switch_skips_background_but_allows_needed_foreground_compaction(
    tmp_path, body_tokens
):
    records = []

    def respond(messages, info):
        return ModelResponse(
            parts=[TextPart("Reviewed.")], usage=RequestUsage(input_tokens=5_600, output_tokens=2)
        )

    model = terra_model(Ledger(tmp_path / "ledger.db"), respond, window=10_000)
    compaction = BackgroundCompaction(record=records.append, keep_tokens=5)
    agent = Agent(model, capabilities=[compaction])
    history = [
        ModelRequest(parts=[UserPromptPart("item" * body_tokens)]),
        ModelResponse(
            parts=[TextPart("Done.")], model_name="deepseek-flash", provider_name="deepseek"
        ),
    ]

    try:
        first = await agent.run(
            "Continue the inbox review",
            message_history=history,
            conversation_id="session",
            run_id="first",
        )
        assert first.output == "Reviewed."
        if body_tokens == 7_000:
            assert [record["stage"] for record in records] == ["ready", "compacted"]
            assert records[-1]["source"] == "foreground"
        else:
            assert records == []
            await agent.run(
                "Review again",
                message_history=first.all_messages(),
                conversation_id="session",
                run_id="second",
            )
            assert records[0]["stage"] == "preparing"
    finally:
        await compaction.close()


async def test_chatgpt_catalog_uses_only_input_window_fields(tmp_path, monkeypatch):
    rows = [
        {"slug": "gpt-6.1-terra", "max_tokens": 16_384},
        {"slug": "gpt-6.1-terra-explicit", "context_window": 272_000, "max_tokens": 32_768},
        {"slug": "catalog-length", "context_window": True, "context_length": 96_000},
        {"slug": "catalog-input", "max_context_length": -1, "max_input_tokens": 120_000},
        {"slug": "unknown-family", "max_tokens": 16_384},
    ]
    client = MagicMock(
        get=AsyncMock(return_value={"models": [dict(row, visibility="list") for row in rows]})
    )
    monkeypatch.setattr(chatgpt, "OpenAIChatGPTProvider", lambda **_: MagicMock(client=client))
    store = chatgpt.ChatGPTStore(tmp_path)

    refreshed = await chatgpt.refresh_models(store)

    assert store.state()["models"] == refreshed
    assert [entry.get("context_window") for entry in refreshed] == [
        None,
        272_000,
        96_000,
        120_000,
        None,
    ]
    assert [chatgpt.chatgpt_context_window(entry) for entry in refreshed] == [
        200_000,
        272_000,
        96_000,
        120_000,
        None,
    ]


@pytest.mark.parametrize("output_limit", [None, 5_000])
async def test_output_limit_reserves_room_for_the_reply(tmp_path, output_limit):
    def respond(messages, info):
        return ModelResponse(parts=[TextPart("Fits.")])

    model = terra_model(
        Ledger(tmp_path / "ledger.db"), respond, window=10_000, output_limit=output_limit
    )
    compaction = BackgroundCompaction(record=lambda _: None, max_fraction=0.95)
    agent = Agent(model, capabilities=[compaction])
    run = agent.run("item" * 8_000, conversation_id="session", run_id="run")

    if output_limit is None:
        assert (await run).output == "Fits."
    else:
        with pytest.raises(WorkingContextBudgetError):
            await run


def test_registry_loads_existing_toml_with_no_output_limit():
    assert all(model.max_output_tokens is None for model in load_registry().models)
