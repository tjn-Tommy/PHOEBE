"""Event service: subscriptions, retained snapshots, replay, bus health.

The in-process face of the future SSE endpoint (plan §6.7): ``snapshot()`` is
``GET /state``, ``subscribe(since_seq=N)`` is the ``Last-Event-ID`` replay
contract, ``bus_stats()`` feeds the diagnostics panel.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Iterable

from ..core.bus import DropPolicy, EventBus, Subscription

if TYPE_CHECKING:
    from ..contracts.events import BusEvent, EventBusStats

#: Topics a generic frontend console listens to.
DEFAULT_TOPICS = (
    "progress", "run_state", "data_pointer", "device_health", "error", "log",
)


class EventService:
    def __init__(self, *, bus: EventBus) -> None:
        self._bus = bus

    async def subscribe(self, topics: Iterable[str] = DEFAULT_TOPICS, *,
                        maxsize: int | None = None,
                        since_seq: int | None = None) -> Subscription:
        """Open a bounded subscription (must be consumed on the core loop).
        ``since_seq`` primes it from the replay ring instead of the retained
        snapshot — reconnect semantics."""
        return self._bus.subscribe(list(topics), maxsize=maxsize,
                                   policy=DropPolicy.DROP_OLDEST,
                                   since_seq=since_seq)

    def unsubscribe(self, sub: Subscription) -> None:
        self._bus.unsubscribe(sub)

    async def snapshot(self, topics: Iterable[str] = DEFAULT_TOPICS
                       ) -> list[BusEvent]:
        """Per-entity retained snapshot in seq order + implicit current seq
        (the last event's ``seq``)."""
        events: list[BusEvent] = []
        for topic in topics:
            events.extend(self._bus.retained_all(topic))
        events.sort(key=lambda e: e.seq)
        return events

    async def replay_since(self, seq: int,
                           topics: Iterable[str] | None = None) -> list[BusEvent]:
        return list(self._bus.replay_since(seq, topics))

    async def bus_stats(self) -> EventBusStats:
        return self._bus.stats()

    @property
    def current_seq(self) -> int:
        return self._bus.current_seq
