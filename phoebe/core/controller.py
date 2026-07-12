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
from collections import deque

# Serializable instrument models were promoted to phoebe.contracts.instruments
# (plan §6.7); re-imported here so pre-promotion import paths keep working.
from ..contracts.instruments import (
    ControllerStats,
    DeviceHealth,
    DeviceIdentity,
    DeviceStatusView,
    InstrumentDescriptor,
    InstrumentSnapshot,
    SnapshotValue,
)
from .capability import CapabilityRegistry
from .contracts import InstrumentId, utc_now

__all__ = [
    "ControllerStats",
    "DeviceHealth",
    "DeviceIdentity",
    "DeviceStatusView",
    "InstrumentController",
    "InstrumentDescriptor",
    "InstrumentSnapshot",
    "SnapshotValue",
]

#: Operational error ring size — capped so an always-on deployment's stats
#: never grow without bound (plan §3.1 A2).
_ERROR_RING_SIZE = 32


class InstrumentController(ABC):
    def __init__(self, instrument_id: InstrumentId,
                 *, validate_responses: bool = True) -> None:
        self.instrument_id = instrument_id
        self.capabilities = CapabilityRegistry(
            owner=str(instrument_id), validate_responses=validate_responses
        )
        self._op_lock = asyncio.Lock()
        self._started_at = utc_now()
        self._ops_ok = 0
        self._ops_failed = 0
        self._error_ring: deque[str] = deque(maxlen=_ERROR_RING_SIZE)

    # ---- operational state (plan §3.1 A2) -----------------------------------
    @property
    def busy(self) -> bool:
        """True while an operation holds the op-lock (lock-free read)."""
        return self._op_lock.locked()

    def note_ok(self) -> None:
        self._ops_ok += 1

    def note_error(self, exc: BaseException | str) -> None:
        self._ops_failed += 1
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        self._error_ring.append(f"{utc_now().isoformat()} {text}")

    def get_stats(self) -> ControllerStats:
        return ControllerStats(
            instrument_id=self.instrument_id, started_at=self._started_at,
            ops_ok=self._ops_ok, ops_failed=self._ops_failed,
            recent_errors=tuple(self._error_ring),
        )

    async def probe_health(self) -> DeviceHealth:
        """``get_health()`` under the op-lock (plan §3.1 A10): probes never
        interleave SCPI into a running acquisition.  A device whose lock is
        held is being actively used — that IS the health signal, so the probe
        is skipped instead of queueing behind the operation."""
        if self._op_lock.locked():
            return DeviceHealth(status="ok", detail="busy (operation in progress)")
        async with self._op_lock:
            return await self.get_health()

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
