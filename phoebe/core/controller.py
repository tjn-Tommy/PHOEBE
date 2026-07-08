"""Controller base: the device's formal facade (refactor.md §4.4).

A controller owns its driver, translates vendor protocol into domain models,
and carries the v2 runtime contracts:

* atomic operation lock — multi-command operations execute under one lock;
* settled semantics — motion/display actions return only when physically done;
* ``stop()`` / ``safe_state()`` — hardware-side cancellation, callable
  concurrently with in-flight operations (the only calls allowed to bypass
  the operation lock);
* ``stage()`` / ``unstage()`` — run-scoped known-state setup/teardown.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import Field

from .capability import CapabilityRegistry
from .contracts import AwareDatetime, ContractModel, InstrumentId, utc_now


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


class InstrumentController(ABC):
    def __init__(self, instrument_id: InstrumentId,
                 *, validate_responses: bool = True) -> None:
        self.instrument_id = instrument_id
        self.capabilities = CapabilityRegistry(
            owner=str(instrument_id), validate_responses=validate_responses
        )
        self._op_lock = asyncio.Lock()

    # ---- lifecycle & observability -----------------------------------------
    @property
    @abstractmethod
    def descriptor(self) -> InstrumentDescriptor: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_identity(self) -> DeviceIdentity: ...

    @abstractmethod
    async def get_health(self) -> DeviceHealth: ...

    @abstractmethod
    async def get_snapshot(self) -> InstrumentSnapshot: ...

    # ---- runtime contracts (v2) ----------------------------------------------
    async def stage(self) -> None:
        """Put the device into a known state before a run (clear error queue,
        set trigger mode, ...).  Default: no-op."""

    async def unstage(self) -> None:
        """Restore idle behaviour after a normal run end (resume keepalive,
        continuous sweep, ...).  Default: no-op."""

    @abstractmethod
    async def stop(self) -> None:
        """Fast-abort the current action, leaving the device recoverable.
        MUST be callable concurrently with an in-flight operation — this is
        the only method allowed to bypass the operation lock."""

    @abstractmethod
    async def safe_state(self) -> None:
        """Unconditionally enter the physically safe state (modulation off,
        shutter closed, outputs off).  Used on failure paths."""
