import asyncio
from collections.abc import AsyncGenerator

from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.server import sse_frame


async def test_stream_reconnect_replays_stored_events_not_live_text(tmp_path):
    log = EventLog(tmp_path / "events.db")
    hub = Hub(log)
    created = hub.publish("owner", "session.created")
    first = hub.subscribe("owner")
    assert isinstance(first, AsyncGenerator)
    try:
        assert await anext(first) == created
        hub.broadcast("owner", "text.delta", {"text": "partial text"})
        live = await asyncio.wait_for(anext(first), 2)
        assert live is not None
        assert live.live
        assert live.seq == created.seq
        assert "id:" not in sse_frame(live)
    finally:
        await first.aclose()

    stored = hub.publish("owner", "queue.paused", {"reason": "owner"})
    reconnected = Hub(EventLog(log.path)).subscribe("owner", after=created.seq)
    assert isinstance(reconnected, AsyncGenerator)
    try:
        replayed = await asyncio.wait_for(anext(reconnected), 2)
        assert replayed == stored
        assert not replayed.live
        assert sse_frame(replayed).startswith(f"id: {stored.seq}\n")
    finally:
        await reconnected.aclose()


async def test_stream_reads_committed_event_after_wakeup_even_with_cached_log(tmp_path):
    log = EventLog(tmp_path / "events.db")
    hub = Hub(log)
    created = hub.publish("owner", "session.created")
    subscription = hub.subscribe("owner", after=created.seq)
    assert isinstance(subscription, AsyncGenerator)
    waiting = asyncio.ensure_future(anext(subscription))
    try:
        await asyncio.sleep(0)
        log.read("owner")
        other_writer = EventLog(log.path)
        stored = other_writer.append("owner", "queue.paused", {"reason": "owner"})
        hub.wake("owner")
        assert await asyncio.wait_for(waiting, 2) == stored
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await subscription.aclose()
