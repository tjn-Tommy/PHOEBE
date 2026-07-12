"""Qt ↔ loop bridging (refactor.md §12.5), PyQt5 edition.

Out of the loop: events MUST cross into the Qt world through a Qt Signal —
cross-thread signal emission is queued onto the Qt main thread, so slots can
touch widgets safely; calling widget methods from the loop thread crashes
randomly.  Into the loop: panels submit CommandEnvelopes via
``services.call(services.runs.submit(envelope))``.

The bridge consumes the EventService (plan §6.1) — the UI never touches the
bus directly.  Import requires PyQt5 (``pip install phoebe[ui]``); the core
never imports this module.
"""
from __future__ import annotations

from collections.abc import Iterable

from PyQt5.QtCore import QObject, pyqtSignal

from ..services import ServiceHub
from ..services.events import DEFAULT_TOPICS


class UiEventBridge(QObject):
    """Pumps service-layer events into a Qt Signal; panels connect with the
    default (auto → queued across threads) connection type."""

    event_received = pyqtSignal(object)          # emits BusEvent instances

    def __init__(self) -> None:
        super().__init__()
        self._pump_future = None

    def start(self, services: ServiceHub,
              topics: Iterable[str] = DEFAULT_TOPICS) -> None:
        topic_list = list(topics)

        async def subscribe_and_pump() -> None:
            # subscribe on the loop thread — the bus is single-threaded by design
            sub = await services.events.subscribe(topic_list)
            try:
                async for event in sub:
                    self.event_received.emit(event)  # queued into the Qt thread
            finally:
                services.events.unsubscribe(sub)

        self._pump_future = services.call(subscribe_and_pump())

    def stop(self) -> None:
        """Cancel the pump before the loop shuts down (call from the Qt side)."""
        if self._pump_future is not None:
            self._pump_future.cancel()
            self._pump_future = None
