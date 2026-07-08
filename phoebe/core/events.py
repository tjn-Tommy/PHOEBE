"""EventBus event contracts (refactor.md §3.4, §8.1).

Events inherit ``ContractModel`` whose strict schema rejects ``np.ndarray``
fields at class-definition time — "no big arrays on the bus" is enforced by
the type system, not by code review.  New event types must be added to the
``GatewayEvent`` closed union before they may leave the process.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator

from .contracts import (
    AwareDatetime,
    ContractModel,
    InstrumentId,
    RunId,
    TaskId,
)

#: Serialized size ceiling for a single bus event (second line of defense;
#: the schema-level list caps below are the first).
MAX_EVENT_JSON_BYTES = 64_000

PREVIEW_MAX_POINTS = 256


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"      # pause requested, takes effect at next checkpoint
    PAUSED = "paused"
    STOPPING = "stopping"    # cancel requested, hardware stop + cleanup running
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"      # cancelled by user

    @property
    def is_terminal(self) -> bool:
        return self in (RunState.COMPLETED, RunState.FAILED, RunState.ABORTED)


class BusEvent(ContractModel):
    schema_version: int = 1
    seq: Annotated[int, Field(ge=0)] = 0   # stamped by the bus at publish time
    task_id: TaskId | None = None
    t_wall: AwareDatetime
    t_mono_ns: int                          # monotonic clock, for data alignment (§10.4)


class TracePreview(ContractModel):
    """Down-sampled preview small enough for the bus; cap is part of the schema."""

    x_nm: list[float] = Field(max_length=PREVIEW_MAX_POINTS)
    y_dbm: list[float] = Field(max_length=PREVIEW_MAX_POINTS)


class DataPointerEvent(BusEvent):
    event_type: Literal["data_pointer"] = "data_pointer"
    run_id: RunId
    dataset: str                 # e.g. "artifacts.h5:/traces/spectrum"
    index: int
    preview: TracePreview | None = None


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
    error_type: str              # InstrumentError subclass name
    message: str
    instrument_id: InstrumentId | None = None


class LogEvent(BusEvent):
    """Short log excerpt for live UI consoles (full log goes to experiment.jsonl)."""

    event_type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str = Field(max_length=2000)


# Closed union used by the Gateway for serialization: an event type that is
# not registered here cannot leave the process.
GatewayEvent = Annotated[
    Union[
        DataPointerEvent,
        ProgressEvent,
        RunStateEvent,
        DeviceHealthEvent,
        ErrorEvent,
        LogEvent,
    ],
    Field(discriminator="event_type"),
]


def topic_of(event: BusEvent) -> str:
    return getattr(event, "event_type", type(event).__name__)
