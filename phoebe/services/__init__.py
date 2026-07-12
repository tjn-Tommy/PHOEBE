"""Application service layer (plan §6.1, PR C-4).

Thin, async, frontend-agnostic facades over the kernel: the PyQt shell calls
these in-process today; the future FastAPI adapter (Phase E) exposes the same
objects over HTTP/SSE.  Panels stop reaching into ``device_manager`` /
``task_manager`` directly — the import-linter UI contract enforces it.

All service methods run on the core loop.  UI threads submit work through
``ServiceHub.call`` (``run_coroutine_threadsafe``) and receive results via
concurrent futures.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from collections.abc import Coroutine

from .config import ConfigService
from .devices import DeviceService
from .events import EventService
from .plugins import PluginService
from .runs import RunService

if TYPE_CHECKING:
    import concurrent.futures

__all__ = [
    "ConfigService",
    "DeviceService",
    "EventService",
    "PluginService",
    "RunService",
    "ServiceHub",
]


@dataclass
class ServiceHub:
    """One object handed to every frontend."""

    runs: RunService
    devices: DeviceService
    events: EventService
    plugins: PluginService
    config: ConfigService
    loop: asyncio.AbstractEventLoop

    def call(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Thread-safe entry point for non-loop threads (the Qt side)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
