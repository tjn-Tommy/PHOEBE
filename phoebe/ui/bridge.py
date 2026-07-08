"""Qt ↔ loop bridging (refactor.md §12.5).

Out of the loop: events MUST cross into the Qt world through a Qt Signal
(QueuedConnection) — touching widgets from the loop thread crashes randomly.
Into the loop: panels submit CommandEnvelopes via
``gateway.submit_threadsafe(envelope, loop)``.

Import requires PySide6 (``pip install phoebe[ui]``); the core never imports
this module.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from PySide6.QtCore import QObject, Signal

from ..core.bus import DropPolicy, EventBus

DEFAULT_UI_TOPICS = (
    "progress", "run_state", "data_pointer", "device_health", "error", "log",
)


class UiEventBridge(QObject):
    """Pumps bus events into a Qt Signal; connect panels with QueuedConnection."""

    event_received = Signal(object)          # emits BusEvent instances

    def start(self, bus: EventBus, loop: asyncio.AbstractEventLoop,
              topics: Iterable[str] = DEFAULT_UI_TOPICS) -> None:
        async def subscribe_and_pump() -> None:
            # subscribe on the loop thread — the bus is single-threaded by design
            sub = bus.subscribe(list(topics), policy=DropPolicy.DROP_OLDEST)
            async for event in sub:
                self.event_received.emit(event)     # Signal.emit is thread-safe

        asyncio.run_coroutine_threadsafe(subscribe_and_pump(), loop)
