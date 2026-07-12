"""phoebe.contracts — everything serializable (evolution plan §7).

The stable wire vocabulary shared by the kernel, the services layer, every
frontend and (later) the HTTP API: contract base types, command/ack models
with typed codes, the event family, the error taxonomy and the run journal
records.  This package has **no phoebe-internal dependencies** — it sits below
``phoebe.core`` in the layer contract, so anything may import it.

``python -m phoebe.contracts.export`` emits the JSON Schema bundle used for
frontend codegen and the CI drift check.
"""
from __future__ import annotations

from .base import (
    AwareDatetime,
    CapabilityId,
    ContractModel,
    InstrumentId,
    LeaseId,
    RunId,
    TaskId,
    timestamps,
    utc_now,
    validate_boundary,
)
from .commands import AckCode, AdmissionCode, AdmissionDecision, CommandAck, CommandEnvelope
from .errors import ErrorCode, ErrorInfo, error_code_of, error_info_of
from .events import GatewayEvent, PreviewPayload
from .run import JournalRecordType, RunJournalRecord, RunResult, RunState

__all__ = [
    "AckCode",
    "AdmissionCode",
    "AdmissionDecision",
    "AwareDatetime",
    "CapabilityId",
    "CommandAck",
    "CommandEnvelope",
    "ContractModel",
    "ErrorCode",
    "ErrorInfo",
    "GatewayEvent",
    "InstrumentId",
    "JournalRecordType",
    "LeaseId",
    "PreviewPayload",
    "RunId",
    "RunJournalRecord",
    "RunResult",
    "RunState",
    "TaskId",
    "error_code_of",
    "error_info_of",
    "timestamps",
    "utc_now",
    "validate_boundary",
]
