"""EventBus event contracts (refactor.md §3.4, §8.1; v2 per plan §6.5).

Events inherit ``ContractModel`` whose strict schema rejects ``np.ndarray``
fields at class-definition time — "no big arrays on the bus" is enforced by
the type system, not by code review.  New event types must be added to the
``GatewayEvent`` closed union before they may leave the process.

v2 additions: the ``PreviewPayload`` discriminated union (spectrum is no
longer privileged), the ``final`` flag on ``RunStateEvent`` (replaces the
``reason == "final"`` magic string) and typed ``ErrorCode`` on ``ErrorEvent``.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from .base import (
    AwareDatetime,
    ContractModel,
    InstrumentId,
    RunId,
    TaskId,
)
from .errors import ErrorCode
from .run import RunState

#: Serialized size ceiling for a single bus event (second line of defense;
#: the schema-level list caps below are the first).
MAX_EVENT_JSON_BYTES = 64_000

PREVIEW_MAX_POINTS = 256


class BusEvent(ContractModel):
    schema_version: int = 2
    seq: Annotated[int, Field(ge=0)] = 0   # stamped by the bus at publish time
    task_id: TaskId | None = None
    t_wall: AwareDatetime
    t_mono_ns: int                          # monotonic clock, for data alignment (§10.4)


# ------------------------------------------------------------------ previews

class SpectrumPreview(ContractModel):
    """Down-sampled preview small enough for the bus; cap is part of the schema."""

    preview_type: Literal["spectrum"] = "spectrum"
    x_nm: list[float] = Field(max_length=PREVIEW_MAX_POINTS)
    y_dbm: list[float] = Field(max_length=PREVIEW_MAX_POINTS)


class WaveformPreview(ContractModel):
    """Time-domain preview (scope/DAQ channels)."""

    preview_type: Literal["waveform"] = "waveform"
    t_s: list[float] = Field(max_length=PREVIEW_MAX_POINTS)
    y: list[float] = Field(max_length=PREVIEW_MAX_POINTS)
    y_unit: str = "V"


class ImageThumbnail(ContractModel):
    """Tiny raster thumbnail (SLM mask, camera frame); PNG, base64-encoded.
    The length cap keeps the event under the bus byte ceiling."""

    preview_type: Literal["image"] = "image"
    width: Annotated[int, Field(ge=1, le=512)]
    height: Annotated[int, Field(ge=1, le=512)]
    png_base64: str = Field(max_length=48_000)


class ScalarSeries(ContractModel):
    """Named scalar-vs-x series (optimizer metric history, power monitor)."""

    preview_type: Literal["scalar_series"] = "scalar_series"
    name: str
    x: list[float] = Field(max_length=PREVIEW_MAX_POINTS)
    y: list[float] = Field(max_length=PREVIEW_MAX_POINTS)


#: Generic preview vehicle (plan §6.5): panels render by discriminator.
PreviewPayload = Annotated[
    SpectrumPreview | WaveformPreview | ImageThumbnail | ScalarSeries,
    Field(discriminator="preview_type"),
]

#: Backwards-compatible alias — the v1 spectrum-only preview name.
TracePreview = SpectrumPreview


# -------------------------------------------------------------------- events

class DataPointerEvent(BusEvent):
    event_type: Literal["data_pointer"] = "data_pointer"
    run_id: RunId
    dataset: str                 # e.g. "artifacts.h5:/traces/spectrum"
    index: int
    preview: PreviewPayload | None = None


class ProgressEvent(BusEvent):
    event_type: Literal["progress"] = "progress"
    step: int
    total: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def _cap_metrics(cls, v: dict[str, float]) -> dict[str, float]:
        if len(v) > 32:
            raise ValueError("progress metrics capped at 32 entries")
        return v


class RunStateEvent(BusEvent):
    event_type: Literal["run_state"] = "run_state"
    state: RunState
    reason: str | None = None
    #: True only on the terminal event published AFTER cleanup finished and
    #: leases were released — anything starting the next run gates on this
    #: flag, never on the first terminal sighting (plan §6.2).
    final: bool = False


class DeviceHealthEvent(BusEvent):
    event_type: Literal["device_health"] = "device_health"
    instrument_id: InstrumentId
    status: Literal["ok", "degraded", "error", "offline"]
    detail: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def _cap_metrics(cls, v: dict[str, float]) -> dict[str, float]:
        if len(v) > 32:
            raise ValueError("health metrics capped at 32 entries")
        return v


class ErrorEvent(BusEvent):
    event_type: Literal["error"] = "error"
    error_type: str              # exception class name (diagnostic)
    message: str
    code: ErrorCode = ErrorCode.INTERNAL
    instrument_id: InstrumentId | None = None


class LogEvent(BusEvent):
    """Short log excerpt for live UI consoles (full log goes to experiment.jsonl)."""

    event_type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str = Field(max_length=2000)


# Closed union used by the Gateway for serialization: an event type that is
# not registered here cannot leave the process.
GatewayEvent = Annotated[
    DataPointerEvent | ProgressEvent | RunStateEvent | DeviceHealthEvent | ErrorEvent | LogEvent,
    Field(discriminator="event_type"),
]


class EventBusStats(ContractModel):
    """Bus health counters (plan §6.5): published to diagnostics consumers
    instead of living only in process memory."""

    current_seq: int
    total_dropped: int
    oversize_dropped: int
    failed_subscriptions: int
    subscriber_count: int


def topic_of(event: BusEvent) -> str:
    return getattr(event, "event_type", type(event).__name__)


def entity_of(event: BusEvent) -> str:
    """Retained-map entity key (plan §6.5): device_health retains one event
    per instrument, run-scoped topics one per task."""
    instrument_id = getattr(event, "instrument_id", None)
    if instrument_id is not None:
        return str(instrument_id)
    if event.task_id is not None:
        return str(event.task_id)
    return ""
