"""SSE event stream (plan E-2): seq-addressed replay + live follow.

Wire format, one frame per bus event::

    id: <seq>
    event: <event_type>
    data: <BusEvent JSON>

Reconnect contract (A4 / plan §6.5): the client resumes with the standard
``Last-Event-ID`` header (or ``since_seq`` query parameter); the subscription
is primed from the bus replay ring, so a dropped connection repairs its gap
losslessly within the ring bound.  A fresh connect (no cursor) is primed from
the per-entity retained snapshot instead — current state first, then live.

Every bus touch runs **on the core loop** via ``ServiceHub.call`` — the
server's event loop never handles core asyncio primitives directly.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..contracts.errors import BusOverflowError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from ..services import ServiceHub

#: Client-side retry hint sent on connect (EventSource semantics).
RETRY_HINT_MS = 3000


async def _next(sub: Any, keepalive_s: float) -> Any:
    """Runs on the core loop: one event, or None after a keepalive interval."""
    try:
        return await asyncio.wait_for(sub.get(), keepalive_s)
    except TimeoutError:
        return None


def _frame(event: Any) -> str:
    return (f"id: {event.seq}\nevent: {event.event_type}\n"
            f"data: {event.model_dump_json()}\n\n")


async def stream_events(
    services: ServiceHub,
    *,
    topics: Sequence[str],
    since_seq: int | None,
    keepalive_s: float,
    limit: int | None = None,
) -> AsyncIterator[str]:
    """The endpoint body.  ``limit`` closes the stream after N events —
    a debugging/curl affordance that also makes buffered-transport tests
    deterministic; browsers stream unbounded (``limit`` unset)."""
    sub = await asyncio.wrap_future(services.call(
        services.events.subscribe(topics, since_seq=since_seq)))
    try:
        yield f"retry: {RETRY_HINT_MS}\n\n"
        sent = 0
        while limit is None or sent < limit:
            try:
                event = await asyncio.wrap_future(
                    services.call(_next(sub, keepalive_s)))
            except BusOverflowError:
                # Subscription failed under backpressure: tell the client to
                # reconnect — its Last-Event-ID repairs the gap from the ring.
                yield "event: stream_reset\ndata: {}\n\n"
                return
            if event is None:
                yield ": keepalive\n\n"
                continue
            yield _frame(event)
            sent += 1
    finally:
        services.loop.call_soon_threadsafe(services.events.unsubscribe, sub)
