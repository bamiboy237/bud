"""In-process hub between the event log and its readers.
Writers publish through the hub, which appends to the log and wakes the readers subscribed
to that session. Streaming text is broadcast live and never stored: the finished reply is
already saved in the run's terminal event.
"""

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from bud.core.events import Event, EventType
from bud.core.log import EventLog

# Poll the log after a missed wake-up; delivery still depends on a successful log read.
WAIT_SECONDS = 15
# A reader that falls this far behind loses live text, never stored events.
MAX_LIVE_BACKLOG = 2_000


class _Subscriber:
    def __init__(self) -> None:
        self.items: deque[tuple[EventType, dict] | int] = deque()
        self.ready = asyncio.Event()

    def push(self, item: tuple[EventType, dict] | int) -> None:
        if isinstance(item, int):
            if self.items and isinstance(self.items[-1], int):
                self.items[-1] = max(self.items[-1], item)
            else:
                self.items.append(item)
        elif len(self.items) < MAX_LIVE_BACKLOG:
            self.items.append(item)
        self.ready.set()


class Hub:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self._subscribers: dict[str, set[_Subscriber]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()

    def wake(self, session_id: str) -> None:
        self._send(session_id, self.log.last_seq(session_id))

    def broadcast(self, session_id: str, type: EventType, data: dict) -> None:
        """Deliver a live-only event to current readers. It is not written to the log."""
        self._send(session_id, (type, data))

    def _send(self, session_id: str, item: tuple[EventType, dict] | int) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._deliver, session_id, item)

    def publish(self, session_id: str, type: EventType, data: dict | None = None) -> Event:
        event = self.log.append(session_id, type, data)
        self.wake(session_id)
        return event

    def publish_once(
        self,
        session_id: str,
        type: EventType,
        data: dict | None = None,
        *,
        key: str | None = None,
    ) -> Event | None:
        event = self.log.append_once(session_id, type, data, key=key)
        if event is not None:
            self.wake(session_id)
        return event

    def _deliver(self, session_id: str, item: tuple[EventType, dict] | int) -> None:
        for subscriber in self._subscribers.get(session_id, ()):
            subscriber.push(item)

    async def subscribe(self, session_id: str, after: int = 0) -> AsyncIterator[Event | None]:
        """Yield stored events after `after` in order, live events as they arrive, and None
        as a keep-alive. A live event carries the last stored seq, so resuming from any
        yielded event's seq never skips a stored one."""
        self.bind_loop()
        subscriber = _Subscriber()
        subscriber.push(self.log.last_seq(session_id))
        self._subscribers[session_id].add(subscriber)
        try:
            while True:
                if not subscriber.items:
                    subscriber.ready.clear()
                    try:
                        await asyncio.wait_for(subscriber.ready.wait(), WAIT_SECONDS)
                    except TimeoutError:
                        subscriber.push(self.log.last_seq(session_id))
                        yield None
                    continue
                item = subscriber.items.popleft()
                if isinstance(item, int):
                    for event in await asyncio.to_thread(self.log.read, session_id, after):
                        # A later stored event must not overtake live text already queued.
                        if event.seq > item:
                            break
                        after = event.seq
                        yield event
                else:
                    type, data = item
                    yield Event(
                        id=uuid4().hex,
                        session_id=session_id,
                        seq=after,
                        type=type,
                        data=data,
                        created_at=datetime.now(UTC),
                        live=True,
                    )
        finally:
            self._subscribers[session_id].discard(subscriber)
            if not self._subscribers[session_id]:
                del self._subscribers[session_id]
