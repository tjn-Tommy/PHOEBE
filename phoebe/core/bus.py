"""In-process observation bus (refactor.md §9; v2 per evolution plan §6.5).

The bus carries only droppable, snapshot-replayable observation streams:
progress, state, health, data pointers, log excerpts.  Commands go through
the Gateway (point-to-point, must-deliver); bulk data goes through the
RunWriter (data plane).  Fan-out is topic → per-subscriber bounded queue
with an explicit drop policy; the publisher is never blocked by a slow UI.

v2 semantics:

* **Per-entity retained state** — retained maps are keyed by (topic, entity):
  ``device_health`` retains one event per instrument, ``run_state`` one per
  task.  A late subscriber receives a true snapshot, not just the single most
  recent event.
* **Seq-addressable replay ring** — a bounded in-memory ring lets a
  reconnecting client ask for "events since seq N" (the SSE ``Last-Event-ID``
  contract at the future service boundary).
* **Safe drop policies** — a ``DropPolicy.ERROR`` overflow fails the
  *subscription* (logged loudly, counted), never the publisher.
* The 64 KB event ceiling is a real check on every publish: oversized events
  are dropped and counted (and raise in dev mode so tests catch the bug).
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from enum import StrEnum
from collections.abc import AsyncIterator, Callable, Iterable

from loguru import logger

from .contracts import Timestamps, timestamps
from .errors import BusOverflowError
from .events import MAX_EVENT_JSON_BYTES, BusEvent, EventBusStats, entity_of, topic_of

#: Ceiling on retained entities per topic — long-lived deployments retain the
#: newest N entities (oldest-inserted evicted) instead of growing unbounded.
_RETAINED_MAX_ENTITIES = 256


class DropPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"    # UI-type subscriptions: keep newest, count drops
    ERROR = "error"                # serious internal subscribers: overflow fails the sub


#: Queue sentinel that wakes a failed subscription's consumer.
_SUBSCRIPTION_FAILED = object()


class Subscription:
    """One subscriber's bounded queue over a set of topics."""

    def __init__(self, topics: tuple[str, ...], maxsize: int, policy: DropPolicy) -> None:
        self.topics = topics
        self.policy = policy
        self.dropped = 0
        self._q: asyncio.Queue[object] = asyncio.Queue(maxsize)
        self._closed = False
        self._failed = False

    @property
    def failed(self) -> bool:
        return self._failed

    def _offer(self, event: BusEvent) -> bool:
        """Enqueue; returns False when the subscription must be failed
        (ERROR-policy overflow) so the bus can detach it."""
        if self._closed:
            return True
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
                return False
        return True

    def _fail(self) -> None:
        """Overflow on an ERROR-policy subscription: close it and wake the
        consumer with a poison item so it observes BusOverflowError."""
        self._failed = True
        self._closed = True
        try:
            self._q.get_nowait()         # make room for the poison item
        except asyncio.QueueEmpty:
            pass
        self._q.put_nowait(_SUBSCRIPTION_FAILED)

    def _check(self, item: object) -> BusEvent:
        if item is _SUBSCRIPTION_FAILED:
            raise BusOverflowError(
                f"subscription {self.topics} overflowed (maxsize={self._q.maxsize}) "
                f"and was failed by the bus"
            )
        return item  # type: ignore[return-value]

    async def get(self) -> BusEvent:
        return self._check(await self._q.get())

    def get_nowait(self) -> BusEvent | None:
        try:
            return self._check(self._q.get_nowait())
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> AsyncIterator[BusEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[BusEvent]:
        while not self._closed or not self._q.empty():
            yield self._check(await self._q.get())


class EventBus:
    """Topic fan-out bus; its data structures are only touched on the loop thread."""

    def __init__(self, *, default_queue_size: int = 256, dev_mode: bool = True,
                 replay_ring_size: int = 2048) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        # topic → entity → latest event (insertion-ordered for eviction)
        self._retained: dict[str, dict[str, BusEvent]] = defaultdict(dict)
        self._ring: deque[BusEvent] = deque(maxlen=replay_ring_size)
        self._default_queue_size = default_queue_size
        self._dev_mode = dev_mode
        self._seq = 0
        self._oversize_dropped = 0
        self._failed_subscriptions = 0
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
        since_seq: int | None = None,
    ) -> Subscription:
        """Subscribe to topics.  A plain subscribe primes the queue with the
        per-entity retained snapshot; ``since_seq`` primes it from the replay
        ring instead (reconnect semantics: "everything after seq N")."""
        sub = Subscription(tuple(topics), maxsize or self._default_queue_size, policy)
        topic_set = set(sub.topics)
        if since_seq is not None:
            for event in self._ring:
                if event.seq > since_seq and topic_of(event) in topic_set:
                    sub._offer(event)
        else:
            snapshot = [e for t in sub.topics for e in self._retained.get(t, {}).values()]
            for event in sorted(snapshot, key=lambda e: e.seq):
                sub._offer(event)
        for t in sub.topics:
            self._subs[t].add(sub)
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
        topic = topic_of(event)
        # Real size check (plan §6.5), not a dev-only assert: an oversized
        # event is dropped and counted; dev mode additionally raises so the
        # producing code is fixed instead of silently degraded.
        payload_len = len(event.model_dump_json())
        if payload_len > MAX_EVENT_JSON_BYTES:
            self._oversize_dropped += 1
            logger.error(
                "bus event {} serialized to {} bytes (> {}); dropped — big data "
                "must go through RunWriter", topic, payload_len, MAX_EVENT_JSON_BYTES,
            )
            if self._dev_mode:
                raise ValueError(
                    f"bus event {topic} serialized to {payload_len} bytes "
                    f"(> {MAX_EVENT_JSON_BYTES}); big data must go through RunWriter"
                )
            return
        retained = self._retained[topic]
        entity = entity_of(event)
        retained.pop(entity, None)             # re-insert → newest position
        retained[entity] = event
        while len(retained) > _RETAINED_MAX_ENTITIES:
            retained.pop(next(iter(retained)))
        self._ring.append(event)
        for sub in tuple(self._subs.get(topic, ())):
            if not sub._offer(event):
                # ERROR-policy overflow: fail the subscription, never the
                # publisher (plan §6.5).
                self._failed_subscriptions += 1
                logger.error(
                    "bus subscription {} overflowed (maxsize={}); failing the "
                    "subscription", sub.topics, sub._q.maxsize,
                )
                self.unsubscribe(sub)
                sub._fail()

    def publish_threadsafe(self, event: BusEvent) -> None:
        if self._loop is None:
            raise RuntimeError("EventBus.bind_loop() must run before threadsafe publish")
        self._loop.call_soon_threadsafe(self.publish, event)

    # -- snapshots / replay ------------------------------------------------------
    def retained(self, topic: str, entity: str | None = None) -> BusEvent | None:
        """Latest retained event on a topic — for ``entity`` when given, else
        the most recently published one (v1-compatible behaviour)."""
        per_entity = self._retained.get(topic)
        if not per_entity:
            return None
        if entity is not None:
            return per_entity.get(entity)
        return next(reversed(per_entity.values()))

    def retained_all(self, topic: str) -> tuple[BusEvent, ...]:
        """Full per-entity snapshot of a topic, in seq order."""
        return tuple(sorted(self._retained.get(topic, {}).values(),
                            key=lambda e: e.seq))

    def replay_since(self, seq: int,
                     topics: Iterable[str] | None = None) -> tuple[BusEvent, ...]:
        """Ring contents with ``event.seq > seq`` (optionally topic-filtered).
        The ring is bounded: a client that fell further behind than the ring
        must resynchronize from the retained snapshot instead."""
        topic_set = set(topics) if topics is not None else None
        return tuple(e for e in self._ring
                     if e.seq > seq and (topic_set is None or topic_of(e) in topic_set))

    @property
    def current_seq(self) -> int:
        return self._seq

    def total_dropped(self) -> int:
        seen: set[int] = set()
        total = 0
        for subs in self._subs.values():
            for sub in subs:
                if id(sub) not in seen:
                    seen.add(id(sub))
                    total += sub.dropped
        return total

    def stats(self) -> EventBusStats:
        """Health counters for diagnostics consumers (plan §6.5)."""
        return EventBusStats(
            current_seq=self._seq,
            total_dropped=self.total_dropped(),
            oversize_dropped=self._oversize_dropped,
            failed_subscriptions=self._failed_subscriptions,
            subscriber_count=len({id(s) for subs in self._subs.values() for s in subs}),
        )


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


def stamp() -> Timestamps:
    """Shorthand for the dual timestamps every event needs."""
    return timestamps()
