"""Strict Pydantic contract layer (refactor.md §3.2; promoted from
``phoebe.core.contracts`` per evolution plan §7).

Every value that crosses a process/serialization boundary — configuration,
events, capability request/response payloads, run metadata — is a
``ContractModel``: immutable, unknown-field-rejecting, strictly typed and
JSON-serializable.  Semantic IDs are ``NewType`` aliases so a ``TaskId`` can
never be passed where an ``InstrumentId`` is expected, and the physical scalar
aliases move range-checking to the very first boundary a value enters.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, UTC
from typing import Annotated, NewType, TypedDict, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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


class ContractModel(BaseModel):
    """Cross-boundary contract base: immutable, no unknown fields, strict types
    (no implicit string→number coercion)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,          # "1.5" is never silently coerced; int→float still allowed
        validate_default=True,
    )


# Semantic IDs — NewType keeps task_id / instrument_id / ... from being crossed.
InstrumentId = NewType("InstrumentId", str)
TaskId = NewType("TaskId", str)
RunId = NewType("RunId", str)
LeaseId = NewType("LeaseId", str)
CapabilityId = NewType("CapabilityId", str)

# Physically-constrained scalars — out-of-range explodes at the first boundary.
Nanometer = Annotated[float, Field(gt=0, lt=20_000)]
Dbm = Annotated[float, Field(ge=-120, le=40)]
Seconds = Annotated[float, Field(gt=0)]
Millisecond = Annotated[float, Field(ge=0)]


def utc_now() -> datetime:
    """Timezone-aware wall-clock 'now' (RFC3339-serializable)."""
    return datetime.now(UTC)


class Timestamps(TypedDict):
    """Return shape of ``timestamps()`` — lets ``**timestamps()`` type-check."""

    t_wall: datetime
    t_mono_ns: int


def timestamps() -> Timestamps:
    """Paired wall + monotonic stamps for every event / data row / checkpoint.

    ``t_wall`` (for humans) can be nudged by NTP; ``t_mono_ns``
    (``time.monotonic_ns()``, for machines) is what causal alignment and
    call-duration analysis rely on (refactor.md §10.4).
    """
    return {"t_wall": utc_now(), "t_mono_ns": time.monotonic_ns()}


ModelT = TypeVar("ModelT", bound=BaseModel)


def validate_boundary(model_cls: type[ModelT], data: object) -> ModelT:
    """Validate dict/serialized data arriving at an architecture boundary.

    JSON-mode validation gives exactly the semantics contracts want: JSON's
    structural conversions (array→tuple, object→nested model, ISO string→
    datetime) are accepted, while cross-type coercions ("1.5"→float) stay
    rejected by strict mode.  In-process model instances pass through as-is.
    """
    if isinstance(data, model_cls):
        return data
    return model_cls.model_validate_json(json.dumps(data, default=str))
