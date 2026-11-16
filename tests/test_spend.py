import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.spend import SpendEntry, SpendLimitExceeded, Spent

from bud.gateway.errors import failure
from bud.gateway.ledger import Ledger
from bud.gateway.registry import Registry
from bud.gateway.spend import SqliteSpendStore, build_spend_limits
from bud.gateway.tracked import TrackedModel
from tests.test_gateway import api_spec


async def test_counter_survives_restart_and_counts_each_response_once(tmp_path):
    path = tmp_path / "spend.db"
    store = SqliteSpendStore(path)
    entry = SpendEntry(key="day", usd=Decimal("0.123456789"), requests=1, token="response")
    await store.add_many([entry, entry])
    totals = await SqliteSpendStore(path).get_many(["day", "unknown"])
    assert totals["day"] == Spent(usd=Decimal("0.123456789"), requests=1)
    assert totals["unknown"] == Spent()
    assert await store.get_many([]) == {}
    assert await store.add_many([]) == {}


def test_budget_failure_has_a_specific_recovery_action():
    result = failure(SpendLimitExceeded("budget spent"))
    assert result["kind"] == "budget"
    assert "BUD_DAILY_USD" in result["next"]
    assert "restart" in result["next"]


async def test_counter_expires_totals_and_deduplication_together(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = SqliteSpendStore(tmp_path / "spend.db", clock=lambda: now)
    entry = SpendEntry(
        key="day", usd=Decimal("1"), requests=1, token="response", ttl=timedelta(days=1)
    )
    await store.add_many([entry])
    now += timedelta(days=1)
    assert (await store.get_many(["day"]))["day"] == Spent()
    assert (await store.add_many([entry]))["day"].usd == Decimal("1")


async def test_failed_batch_rolls_back_totals_and_response_claims(tmp_path):
    store = SqliteSpendStore(tmp_path / "spend.db")
    good = SpendEntry(key="day", usd=Decimal("1"), token="response")
    invalid = SpendEntry(key="other", tokens=2**80)
    with pytest.raises(OverflowError):
        await store.add_many([good, invalid])
    assert (await store.get_many(["day"]))["day"] == Spent()
    assert (await store.add_many([good]))["day"].usd == Decimal("1")


async def test_concurrent_stores_do_not_lose_increments(tmp_path):
    stores = [SqliteSpendStore(tmp_path / "spend.db") for _ in range(2)]
    await asyncio.gather(
        *(
            stores[index % 2].add_many(
                [SpendEntry(key="day", usd=Decimal("0.1"), requests=1, token=str(index))]
            )
            for index in range(16)
        )
    )
    assert (await stores[0].get_many(["day"]))["day"] == Spent(usd=Decimal("1.6"), requests=16)


async def test_daily_cap_uses_route_prices_and_survives_new_agent(tmp_path):
    specs = [
        api_spec("same-name", input=1, output=0, cached=0),
        api_spec("same-name", provider="deepseek", input=9, output=0, cached=0),
    ]
    registry = Registry(models=specs)
    calls = []

    def respond(messages, info):
        calls.append(True)
        return ModelResponse(
            parts=[TextPart("done")],
            model_name="same-name",
            usage=RequestUsage(input_tokens=1_000_000),
        )

    model = TrackedModel(FunctionModel(respond), specs[0], Ledger(tmp_path / "ledger.db"))
    path = tmp_path / "spend.db"
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def agent():
        limits = build_spend_limits(path, registry, Decimal("1.5"))
        assert limits is not None
        limits.clock = lambda: now
        return Agent(model, capabilities=[limits])

    first = agent()
    await first.run("one")
    await first.run("two")
    with pytest.raises(SpendLimitExceeded):
        await agent().run("must not reach the model")
    assert len(calls) == 2
    now += timedelta(days=1)
    await agent().run("new UTC day")
    assert len(calls) == 3
    assert build_spend_limits(path, registry, Decimal(0)) is None
