"""Compatibility shim: the event contracts moved to ``phoebe.contracts.events``
(evolution plan §7 promotion; ``RunState`` now lives in
``phoebe.contracts.run``).  Import from ``phoebe.contracts`` in new code; this
module keeps every pre-promotion import path working for one release.
"""
from __future__ import annotations

from ..contracts.events import (
    MAX_EVENT_JSON_BYTES,
    PREVIEW_MAX_POINTS,
    BusEvent,
    DataPointerEvent,
    DeviceHealthEvent,
    ErrorEvent,
    EventBusStats,
    GatewayEvent,
    ImageThumbnail,
    LogEvent,
    PreviewPayload,
    ProgressEvent,
    RunStateEvent,
    ScalarSeries,
    SpectrumPreview,
    TracePreview,
    WaveformPreview,
    entity_of,
    topic_of,
)
from ..contracts.run import RunState

__all__ = [
    "MAX_EVENT_JSON_BYTES",
    "PREVIEW_MAX_POINTS",
    "BusEvent",
    "DataPointerEvent",
    "DeviceHealthEvent",
    "ErrorEvent",
    "EventBusStats",
    "GatewayEvent",
    "ImageThumbnail",
    "LogEvent",
    "PreviewPayload",
    "ProgressEvent",
    "RunState",
    "RunStateEvent",
    "ScalarSeries",
    "SpectrumPreview",
    "TracePreview",
    "WaveformPreview",
    "entity_of",
    "topic_of",
]
