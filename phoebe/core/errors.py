"""Unified error model (refactor.md §15).

Drivers keep full vendor error detail; Controllers map everything they raise
across the boundary onto this hierarchy, preserving the resource name, the
last command and the original exception as diagnostic context.
"""
from __future__ import annotations


class InstrumentError(Exception):
    """Base class for all instrument-domain errors."""

    def __init__(self, message: str, *, instrument_id: str | None = None,
                 last_command: str | None = None) -> None:
        super().__init__(message)
        self.instrument_id = instrument_id
        self.last_command = last_command


class InstrumentConnectionError(InstrumentError):
    """Transport could not be opened or dropped unexpectedly."""


class InstrumentTimeoutError(InstrumentError):
    """Device did not respond within the deadline."""


class InstrumentProtocolError(InstrumentError):
    """Reply could not be parsed / did not match the vendor protocol."""


class DeviceReportedError(InstrumentError):
    """The device itself reported an error (SCPI error queue, status code)."""


class InvalidInstrumentStateError(InstrumentError):
    """Operation not legal in the device's current state."""


class SafetyViolationError(InstrumentError):
    """Request would violate a physical safety constraint."""


class UnsupportedCapabilityError(InstrumentError):
    """Capability not provided by this device."""

    def __init__(self, capability_id: str, *, owner: str | None = None) -> None:
        super().__init__(
            f"capability {capability_id!r} is not provided by {owner or 'this device'}",
            instrument_id=owner,
        )
        self.capability_id = capability_id


class InstrumentContractError(InstrumentError):
    """Contract validation failed at a boundary (frame spec, schema, ...)."""


class UnsupportedInstrumentModelError(InstrumentError):
    """No factory registered for the configured kind/vendor/model."""


class CapabilityContractError(InstrumentError):
    """A local caller passed the wrong request type to a capability."""

    def __init__(self, capability_id: str, got: type) -> None:
        super().__init__(
            f"capability {capability_id!r} called with wrong request type {got.__name__}"
        )
        self.capability_id = capability_id


# --- resource / task layer (not instrument errors) -------------------------

class LeaseUnavailableError(Exception):
    """A required instrument is already leased by another task."""

    def __init__(self, instrument_id: str, *, holder: str | None = None) -> None:
        super().__init__(f"instrument {instrument_id!r} is locked"
                         + (f" by {holder}" if holder else ""))
        self.instrument_id = instrument_id
        self.holder = holder


class CancelledByUser(Exception):
    """Raised inside a run when the user cancels; maps to RunState.ABORTED."""


class BusOverflowError(RuntimeError):
    """A DropPolicy.ERROR subscription overflowed — indicates an internal bug."""


class PhoebeConfigError(Exception):
    """Configuration could not be parsed / validated at startup."""
