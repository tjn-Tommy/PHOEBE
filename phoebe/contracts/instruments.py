"""Serializable instrument-facing models (promoted from ``core.controller``
per plan §6.7: device stats/health/identity must be wire-ready before any
web work)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import AwareDatetime, ContractModel, InstrumentId, utc_now


class InstrumentDescriptor(ContractModel):
    instrument_id: InstrumentId
    kind: str
    vendor: str
    model: str
    provides: tuple[str, ...]        # declared base capabilities, e.g. ("spectrum_analyzer",)


class DeviceIdentity(ContractModel):
    vendor: str
    model: str
    serial: str = ""
    firmware: str = ""
    raw: str = ""                    # full *IDN? / SDK identity string


class DeviceHealth(ContractModel):
    status: Literal["ok", "degraded", "error", "offline"]
    detail: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


SnapshotValue = str | float | int | bool | None


class InstrumentSnapshot(ContractModel):
    """Settings snapshot for pre/post-run baselines (refactor.md §10.5)."""

    instrument_id: InstrumentId
    taken_at: AwareDatetime = Field(default_factory=utc_now)
    values: dict[str, SnapshotValue] = Field(default_factory=dict)


class ControllerStats(ContractModel):
    """Operational state for device panels / the future API (plan §3.1 A2)."""

    instrument_id: InstrumentId
    started_at: AwareDatetime
    ops_ok: int = 0
    ops_failed: int = 0
    recent_errors: tuple[str, ...] = ()      # newest last, ring-capped


class DeviceStatusView(ContractModel):
    """One device-table row for panels/clients: static config + live
    lifecycle + operational stats, assembled by the device service."""

    instrument_id: InstrumentId
    kind: str
    vendor: str
    model: str
    role: str
    backend: str
    lifecycle: str                   # DeviceLifecycleState value
    detail: str | None = None
    stats: ControllerStats | None = None
