"""In-process observation bus (refactor.md §9).

The bus carries only droppable, snapshot-replayable observation streams:
progress, state, health, data pointers, log excerpts.  Commands go through
the Gateway (point-to-point, must-deliver); bulk data goes through the
RunWriter (data plane).  Fan-out is topic → per-subscriber bounded queue
with an explicit drop policy; the publisher is never blocked by a slow UI.
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from enum import StrEnum
from typing import AsyncIterator, Callable, Iterable

from .contracts import timestamps
from .errors import BusOverflowError
from .events import MAX_EVENT_JSON_BYTES, BusEvent, topic_of


class DropPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"    # UI-type subscriptions: keep newest, count drops
    ERROR = "error"                # serious internal subscribers: overflow = bug


class Subscription:
    """One subscriber's bounded queue over a set of topics."""

    def __init__(self, topics: tuple[str, ...], maxsize: int, policy: DropPolicy) -> None:
        self.topics = topics
        self.policy = policy
        self.dropped = 0
        self._q: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize)
        self._closed = False

    def _offer(self, event: BusEvent) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            if self.policy is DropPolicy.DROP_OLDEST:
                try:
                    self._q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._q.put_nowait(event)
                self.dropped += 1        # exposed as a health metric
            else:
                raise BusOverflowError(
                    f"subscription {self.topics} overflowed (maxsize={self._q.maxsize})"
                )

    async def get(self) -> BusEvent:
        return await self._q.get()

    def get_nowait(self) -> BusEvent | None:
        try:
            return self._q.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> AsyncIterator[BusEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[BusEvent]:
        while not self._closed:
            yield await self._q.get()


class EventBus:
    """Topic fan-out bus; its data structures are only touched on the loop thread."""

    def __init__(self, *, default_queue_size: int = 256, dev_mode: bool = True) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._retained: dict[str, BusEvent] = {}
        self._default_queue_size = default_queue_size
        self._dev_mode = dev_mode
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Record which loop/thread owns the bus (for the cross-thread assert)."""
        self._loop = loop or asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()

    # -- subscribe ------------------------------------------------------------
    def subscribe(
        self,
        topics: Iterable[str],
        *,
        maxsize: int | None = None,
        policy: DropPolicy = DropPolicy.DROP_OLDEST,
    ) -> Subscription:
        sub = Subscription(tuple(topics), maxsize or self._default_queue_size, policy)
        for t in sub.topics:
            self._subs[t].add(sub)
            if t in self._retained:            # late subscriber: snapshot first, then stream
                sub._offer(self._retained[t])
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        for t in sub.topics:
            self._subs[t].discard(sub)

    # -- publish ---------------------------------------------------------------
    def publish(self, event: BusEvent) -> None:
        """Synchronous, non-blocking, O(#subscribers); loop thread only."""
        if self._loop_thread_id is not None:
            assert threading.get_ident() == self._loop_thread_id, (
                "cross-thread publish must go through publish_threadsafe"
            )
        self._seq += 1
        event = event.model_copy(update={"seq": self._seq})
        if self._dev_mode:
            payload = event.model_dump_json()
            assert len(payload) <= MAX_EVENT_JSON_BYTES, (
                f"bus event {topic_of(event)} serialized to {len(payload)} bytes "
                f"(> {MAX_EVENT_JSON_BYTES}); big data must go through RunWriter"
            )
        topic = topic_of(event)
        self._retained[topic] = event
        for sub in tuple(self._subs.get(topic, ())):
            sub._offer(event)

    def publish_threadsafe(self, event: BusEvent) -> None:
        if self._loop is None:
            raise RuntimeError("EventBus.bind_loop() must run before threadsafe publish")
        self._loop.call_soon_threadsafe(self.publish, event)

    def retained(self, topic: str) -> BusEvent | None:
        return self._retained.get(topic)

    def total_dropped(self) -> int:
        seen: set[int] = set()
        total = 0
        for subs in self._subs.values():
            for sub in subs:
                if id(sub) not in seen:
                    seen.add(id(sub))
                    total += sub.dropped
        return total


class ThrottledEmitter:
    """Publisher-side trailing-edge throttle (refactor.md §9.3).

    Within ``min_interval_s`` only the most recent event per topic is kept and
    published when the interval elapses; the first event in a quiet period is
    published immediately.
    """

    def __init__(self, bus: EventBus, min_interval_s: float = 0.1,
                 *, clock: Callable[[], float] | None = None) -> None:
        self._bus = bus
        self._interval = min_interval_s
        self._last_pub: dict[str, float] = {}
        self._pending: dict[str, BusEvent] = {}
        self._handles: dict[str, asyncio.TimerHandle] = {}
        self._clock = clock

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_event_loop().time()

    def emit(self, event: BusEvent) -> None:
        topic = topic_of(event)
        now = self._now()
        last = self._last_pub.get(topic, -1e18)
        if now - last >= self._interval:
            self._last_pub[topic] = now
            self._bus.publish(event)
            return
        # keep only the latest event; arm one trailing-edge flush per topic
        self._pending[topic] = event
        if topic not in self._handles:
            delay = self._interval - (now - last)
            loop = asyncio.get_event_loop()
            self._handles[topic] = loop.call_later(delay, self._flush, topic)

    def _flush(self, topic: str) -> None:
        self._handles.pop(topic, None)
        event = self._pending.pop(topic, None)
        if event is not None:
            self._last_pub[topic] = self._now()
            self._bus.publish(event)

    def flush_all(self) -> None:
        """Publish anything still pending (called at run end so the final
        progress event is never lost to the throttle window)."""
        for topic in list(self._pending):
            handle = self._handles.pop(topic, None)
            if handle is not None:
                handle.cancel()
            self._flush(topic)


def stamp() -> dict[str, object]:
    """Shorthand for the dual timestamps every event needs."""
    return timestamps()
