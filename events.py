"""SSE broadcaster.

One bounded asyncio.Queue per connected client. The queue layer (playlist.py)
calls `publish(event_type, snapshot)` after every mutation; we fan out to all
clients. Every event carries a full QueueState snapshot — the client just
replaces its UI state on each message, no diffing.

Slow client policy: if a client's queue is full, drop its OLDEST event and
push the new one. Since each event is a full snapshot, the dropped event has
no truth in it the new one doesn't already carry. The client only loses the
event-type cue (useful for transient UI flourishes) for that gap.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("smarttube-playlist.events")

CLIENT_QUEUE_MAXSIZE = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClientStream:
    """Async iterator + context manager for one SSE client.

    Registration happens in `Broadcaster.subscribe()` (synchronously) so any
    `publish()` after subscribe-returns reaches this client even before the
    iterator is awaited. Cleanup happens in `aclose()`, which Starlette/
    sse-starlette calls when the connection closes.
    """

    def __init__(self, broadcaster: "Broadcaster", q: asyncio.Queue, initial: Optional[dict]):
        self._bc = broadcaster
        self._q = q
        self._initial: Optional[dict] = (
            {"type": "state", "ts": _now_iso(), "state": initial}
            if initial is not None
            else None
        )
        self._closed = False

    def __aiter__(self) -> "ClientStream":
        return self

    async def __anext__(self) -> dict:
        if self._closed:
            raise StopAsyncIteration
        if self._initial is not None:
            event, self._initial = self._initial, None
            return event
        return await self._q.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bc._unregister(self._q)

    async def __aenter__(self) -> "ClientStream":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


class Broadcaster:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict]] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def publish(self, event_type: str, snapshot: dict) -> None:
        event = {"type": event_type, "ts": _now_iso(), "state": snapshot}
        for q in list(self._clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, push new. Each event is a full snapshot, so
                # losing intermediate ones loses event-type cues only.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def subscribe(self, *, initial: Optional[dict] = None) -> ClientStream:
        """Register a new client. Returns an async iterator + context manager.
        The client's queue is registered synchronously here so any subsequent
        publish() reaches it even before iteration begins."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAXSIZE)
        self._clients.add(q)
        log.debug("SSE client connected (now %d)", len(self._clients))
        return ClientStream(self, q, initial)

    def _unregister(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)
        log.debug("SSE client disconnected (now %d)", len(self._clients))
