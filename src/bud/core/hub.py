"""In-process hub between the event log and its readers.
Writers publish through the hub, which appends to the log and wakes the readers subscribed
to that session.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator

from bud.core.events import Event, EventType
from bud.core.log import EventLog

# A missed wake-up costs at most this much latency, never data, because the log is re-read.
WAIT_SECONDS = 15


class Hub:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    def publish(self, session_id: str, type: EventType, data: dict | None = None) -> Event:
        event = self.log.append(session_id, type, data)
        self._wake(session_id)
        return event

    def publish_once(
        self, session_id: str, type: EventType, data: dict | None = None
    ) -> Event | None:
        event = self.log.append_once(session_id, type, data)
        if event is not None:
            self._wake(session_id)
        return event

    def _wake(self, session_id: str) -> None:
        for waiter in self._waiters.get(session_id, ()):
            waiter.set()

    async def subscribe(self, session_id: str, after: int = 0) -> AsyncIterator[Event | None]:
        waiter = asyncio.Event()
        self._waiters[session_id].add(waiter)
        try:
            while True:
                waiter.clear()
                events = self.log.read(session_id, after)
                for event in events:
                    after = event.seq
                    yield event
                if events:
                    continue
                try:
                    await asyncio.wait_for(waiter.wait(), WAIT_SECONDS)
                except TimeoutError:
                    yield None
        finally:
            self._waiters[session_id].discard(waiter)
            if not self._waiters[session_id]:
                del self._waiters[session_id]
