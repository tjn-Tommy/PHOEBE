"""Unified error model (refactor.md §15) + typed error codes (plan §6.4).

Drivers keep full vendor error detail; Controllers map everything they raise
across the boundary onto this hierarchy, preserving the resource name, the
last command and the original exception as diagnostic context.

``ErrorCode`` / ``ErrorInfo`` are the serialized projection: every error that
reaches a frontend carries a stable machine-readable code — clients never
parse prose (plan §5.2).
"""
from __future__ import annotations

from enum import StrEnum

from .base import ContractModel, InstrumentId


class InstrumentError(Exception):
    """Base class for all instrument-domain errors.

    ``fatal=True`` marks an error that retrying cannot fix (bad resource
    address, missing DLL, identity mismatch) — the retry/reconnect machinery
    surfaces it immediately instead of backing off (plan §3.1 A3).
    """

    def __init__(self, message: str, *, instrument_id: str | None = None,
                 last_command: str | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.instrument_id = instrument_id
        self.last_command = last_command
        self.fatal = fatal


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


class DeviceNotReadyError(Exception):
    """A required instrument's lifecycle state is not READY (plan §6.3):
    lease acquisition is only legal on a connected, identity-verified device."""

    def __init__(self, instrument_id: str, state: str) -> None:
        super().__init__(f"instrument {instrument_id!r} is not ready "
                         f"(lifecycle state: {state})")
        self.instrument_id = instrument_id
        self.state = state


class CancelledByUser(Exception):
    """Raised inside a run when the user cancels; maps to RunState.ABORTED."""


class BusOverflowError(RuntimeError):
    """A DropPolicy.ERROR subscription overflowed — the subscription is failed
    (never the publisher; plan §6.5)."""


class WriterFailedError(RuntimeError):
    """The run's data-plane writer died; producers fail fast instead of hanging."""


class PhoebeConfigError(Exception):
    """Configuration could not be parsed / validated at startup."""


# --------------------------------------------------------- typed error codes

class ErrorCode(StrEnum):
    """Stable machine-readable error classes for the wire (plan §6.4).

    Values are part of the serialized contract — never rename them.
    """

    CONNECTION = "connection"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    DEVICE_REPORTED = "device_reported"
    INVALID_STATE = "invalid_state"
    SAFETY = "safety"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONTRACT = "contract"
    UNSUPPORTED_MODEL = "unsupported_model"
    LEASE_UNAVAILABLE = "lease_unavailable"
    DEVICE_NOT_READY = "device_not_ready"
    CANCELLED = "cancelled"
    BUS_OVERFLOW = "bus_overflow"
    WRITER_FAILED = "writer_failed"
    CONFIG = "config"
    INTERNAL = "internal"


# Ordered mapping: first matching isinstance wins, so subclasses must precede
# their bases (classification by TYPE, never by message text — plan §3.3).
_CODE_BY_TYPE: tuple[tuple[type[BaseException], ErrorCode], ...] = (
    (InstrumentConnectionError, ErrorCode.CONNECTION),
    (InstrumentTimeoutError, ErrorCode.TIMEOUT),
    (InstrumentProtocolError, ErrorCode.PROTOCOL),
    (DeviceReportedError, ErrorCode.DEVICE_REPORTED),
    (InvalidInstrumentStateError, ErrorCode.INVALID_STATE),
    (SafetyViolationError, ErrorCode.SAFETY),
    (UnsupportedCapabilityError, ErrorCode.UNSUPPORTED_CAPABILITY),
    (CapabilityContractError, ErrorCode.CONTRACT),
    (InstrumentContractError, ErrorCode.CONTRACT),
    (UnsupportedInstrumentModelError, ErrorCode.UNSUPPORTED_MODEL),
    (LeaseUnavailableError, ErrorCode.LEASE_UNAVAILABLE),
    (DeviceNotReadyError, ErrorCode.DEVICE_NOT_READY),
    (CancelledByUser, ErrorCode.CANCELLED),
    (BusOverflowError, ErrorCode.BUS_OVERFLOW),
    (WriterFailedError, ErrorCode.WRITER_FAILED),
    (PhoebeConfigError, ErrorCode.CONFIG),
    (ConnectionError, ErrorCode.CONNECTION),
    (TimeoutError, ErrorCode.TIMEOUT),
)


def error_code_of(exc: BaseException) -> ErrorCode:
    """Map an exception to its stable wire code (type-based, subclass-aware)."""
    for exc_type, code in _CODE_BY_TYPE:
        if isinstance(exc, exc_type):
            return code
    return ErrorCode.INTERNAL


class ErrorInfo(ContractModel):
    """Structured error attached to acks and events (plan §6.4): clients read
    ``code`` and ``instrument_id``; ``message`` is human detail only."""

    code: ErrorCode
    message: str
    error_type: str = ""                      # exception class name (diagnostic)
    instrument_id: InstrumentId | None = None


def error_info_of(exc: BaseException) -> ErrorInfo:
    raw = getattr(exc, "instrument_id", None)
    return ErrorInfo(
        code=error_code_of(exc),
        message=str(exc),
        error_type=type(exc).__name__,
        instrument_id=InstrumentId(raw) if isinstance(raw, str) else None,
    )
