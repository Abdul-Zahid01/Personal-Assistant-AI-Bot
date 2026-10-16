"""In-process publish/subscribe for live updates.

Open browser tabs subscribe through a Server-Sent Events stream. When a reminder fires ("message") or data changes
("changed"), every tab hears about it at once, instead of asking the server every few seconds.
Single-process by design, like the scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Protocol

log = logging.getLogger(__name__)

_CLOSE = object()


class EventBus:
    def __init__(self, max_queue: int = 100):
        self._subs: set[asyncio.Queue] = set()
        self._max = max_queue

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, type_: str, **data: Any) -> None:
        event = {"type": type_, **data}
        for q in list(self._subs):
            if q.full():  # a stuck tab must not grow memory forever: drop its oldest event
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    def close(self) -> None:
        """Ask every open stream to finish (used at shutdown so the server can exit promptly)."""
        for q in list(self._subs):
            try:
                q.put_nowait(_CLOSE)
            except asyncio.QueueFull:
                pass


class DisconnectCheck(Protocol):
    async def is_disconnected(self) -> bool: ...


async def sse_stream(bus: EventBus, request: DisconnectCheck, heartbeat: float = 20.0) -> AsyncIterator[str]:
    """Server-Sent Events: 'event: <type>' + JSON data, with a comment line as heartbeat to keep proxies open."""
    q = bus.subscribe()
    try:
        yield "retry: 3000\n\n"
        yield 'event: hello\ndata: {"type": "hello"}\n\n'
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(q.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if event is _CLOSE:
                return
            yield f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
    finally:
        bus.unsubscribe(q)
