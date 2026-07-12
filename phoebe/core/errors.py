"""Compatibility shim: the error taxonomy moved to ``phoebe.contracts.errors``
(evolution plan §7 promotion).  Import from ``phoebe.contracts`` in new code;
this module keeps every pre-promotion import path working for one release.
"""
from __future__ import annotations

from ..contracts.errors import (
    BusOverflowError,
    CancelledByUser,
    CapabilityContractError,
    DeviceNotReadyError,
    DeviceReportedError,
    ErrorCode,
    ErrorInfo,
    InstrumentConnectionError,
    InstrumentContractError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentTimeoutError,
    InvalidInstrumentStateError,
    LeaseUnavailableError,
    PhoebeConfigError,
    SafetyViolationError,
    UnsupportedCapabilityError,
    UnsupportedInstrumentModelError,
    WriterFailedError,
    error_code_of,
    error_info_of,
)

__all__ = [
    "BusOverflowError",
    "CancelledByUser",
    "CapabilityContractError",
    "DeviceNotReadyError",
    "DeviceReportedError",
    "ErrorCode",
    "ErrorInfo",
    "InstrumentConnectionError",
    "InstrumentContractError",
    "InstrumentError",
    "InstrumentProtocolError",
    "InstrumentTimeoutError",
    "InvalidInstrumentStateError",
    "LeaseUnavailableError",
    "PhoebeConfigError",
    "SafetyViolationError",
    "UnsupportedCapabilityError",
    "UnsupportedInstrumentModelError",
    "WriterFailedError",
    "error_code_of",
    "error_info_of",
]
