"""Qt ↔ loop bridging (refactor.md §12.5), PyQt5 edition.

Out of the loop: events MUST cross into the Qt world through a Qt Signal —
cross-thread signal emission is queued onto the Qt main thread, so slots can
touch widgets safely; calling widget methods from the loop thread crashes
randomly.  Into the loop: panels submit CommandEnvelopes via
``gateway.submit_threadsafe(envelope, loop)``.

Import requires PyQt5 (``pip install phoebe[ui]``); the core never imports
this module.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from PyQt5.QtCore import QObject, pyqtSignal

from ..core.bus import DropPolicy, EventBus

DEFAULT_UI_TOPICS = (
    "progress", "run_state", "data_pointer", "device_health", "error", "log",
)


class UiEventBridge(QObject):
    """Pumps bus events into a Qt Signal; panels connect with the default
    (auto → queued across threads) connection type."""

    event_received = pyqtSignal(object)          # emits BusEvent instances

    def __init__(self) -> None:
        super().__init__()
        self._pump_future = None

    def start(self, bus: EventBus, loop: asyncio.AbstractEventLoop,
              topics: Iterable[str] = DEFAULT_UI_TOPICS) -> None:
        async def subscribe_and_pump() -> None:
            # subscribe on the loop thread — the bus is single-threaded by design
            sub = bus.subscribe(list(topics), policy=DropPolicy.DROP_OLDEST)
            try:
                async for event in sub:
                    self.event_received.emit(event)  # queued into the Qt thread
            finally:
                bus.unsubscribe(sub)

        self._pump_future = asyncio.run_coroutine_threadsafe(
            subscribe_and_pump(), loop)

    def stop(self) -> None:
        """Cancel the pump before the loop shuts down (call from the Qt side)."""
        if self._pump_future is not None:
            self._pump_future.cancel()
            self._pump_future = None
