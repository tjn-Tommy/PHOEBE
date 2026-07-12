"""Loguru → EventBus ``LogEvent`` sink (plan §6.5, A4-adoption).

A bounded, level-filtered bridge that makes ``ctx.log`` output visible in
every frontend console; the full structured log keeps going to
``experiment.jsonl`` — the bus only carries short excerpts.

The sink may fire on ANY thread (loguru follows the caller), so it always
publishes through ``bus.publish_threadsafe`` — the loop-safe path (this is
the cross-thread ``put_nowait`` bug from the AstrBot review, fixed by
design).  Publishing a LogEvent never logs, so the bridge cannot recurse.
"""
from __future__ import annotations

from typing import Any
from collections.abc import Callable

from loguru import logger

from .bus import EventBus
from .contracts import TaskId, timestamps
from .events import LogEvent

#: Loguru level → LogEvent level (closed vocabulary).
_LEVEL_MAP = {
    "TRACE": "debug", "DEBUG": "debug",
    "INFO": "info", "SUCCESS": "info",
    "WARNING": "warning",
    "ERROR": "error", "CRITICAL": "error",
}

_MAX_MESSAGE_LEN = 500


class BusLogSink:
    """The sink callable handed to ``logger.add``.  ``redact`` lets a
    deployment scrub secrets/serials before anything reaches a frontend."""

    def __init__(self, bus: EventBus, *,
                 redact: Callable[[str], str] | None = None) -> None:
        self._bus = bus
        self._redact = redact

    def __call__(self, message: Any) -> None:
        record = message.record
        if record["extra"].get("no_bus"):
            return
        text = record["message"]
        if self._redact is not None:
            text = self._redact(text)
        raw_task = record["extra"].get("task_id")
        try:
            self._bus.publish_threadsafe(LogEvent(
                task_id=TaskId(raw_task) if isinstance(raw_task, str) else None,
                level=_LEVEL_MAP.get(record["level"].name, "info"),  # type: ignore[arg-type]
                message=text[:_MAX_MESSAGE_LEN],
                **timestamps(),
            ))
        except RuntimeError:
            pass                            # bus loop not bound yet / shut down


def attach_log_bridge(bus: EventBus, *, level: str = "INFO",
                      redact: Callable[[str], str] | None = None) -> int:
    """Install the bridge; returns the loguru sink id (remove on shutdown).
    ``enqueue=False``: the sink itself is non-blocking (call_soon_threadsafe)
    and the bus's bounded per-subscriber queues do the buffering."""
    return logger.add(BusLogSink(bus, redact=redact), level=level,
                      enqueue=False, format="{message}")
