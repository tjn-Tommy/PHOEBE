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

from .api import ApiEnvelope, ApiError, ApiErrorCode, ServerMeta
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
from .plugin import PluginManifest, PluginStatusView, manifest_hash
from .run import JournalRecordType, RunJournalRecord, RunResult, RunState

__all__ = [
    "AckCode",
    "AdmissionCode",
    "AdmissionDecision",
    "ApiEnvelope",
    "ApiError",
    "ApiErrorCode",
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
    "PluginManifest",
    "PluginStatusView",
    "PreviewPayload",
    "RunId",
    "RunJournalRecord",
    "RunResult",
    "RunState",
    "ServerMeta",
    "TaskId",
    "error_code_of",
    "error_info_of",
    "manifest_hash",
    "timestamps",
    "utc_now",
    "validate_boundary",
]
