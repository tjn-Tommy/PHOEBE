"""Compatibility shim: the contract base layer moved to ``phoebe.contracts.base``
(evolution plan §7 promotion).  Import from ``phoebe.contracts`` in new code;
this module keeps every pre-promotion import path working for one release.
"""
from __future__ import annotations

from ..contracts.base import (
    AwareDatetime,
    CapabilityId,
    ContractModel,
    Dbm,
    InstrumentId,
    LeaseId,
    Millisecond,
    Nanometer,
    RunId,
    Seconds,
    TaskId,
    Timestamps,
    timestamps,
    utc_now,
    validate_boundary,
)

__all__ = [
    "AwareDatetime",
    "ContractModel",
    "InstrumentId",
    "TaskId",
    "RunId",
    "LeaseId",
    "CapabilityId",
    "Nanometer",
    "Dbm",
    "Seconds",
    "Millisecond",
    "Timestamps",
    "utc_now",
    "timestamps",
    "validate_boundary",
]
