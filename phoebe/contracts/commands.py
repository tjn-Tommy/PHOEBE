"""Command-path contracts: envelope, typed ack codes, admission decisions
(plan §6.4).

Every dispatch outcome is a stable ``AckCode``; free text lives in ``reason``
as human detail only — the UI and any future HTTP client parse **zero prose**.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from .base import AwareDatetime, ContractModel, TaskId, utc_now
from .errors import ErrorInfo


class CommandEnvelope(ContractModel):
    command_id: str
    command: str                       # "start_tpa_run" / "pause" / "cancel"
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str = "local_ui"
    t_wall: AwareDatetime = Field(default_factory=utc_now)


class AckCode(StrEnum):
    """Stable dispatch/ack vocabulary (plan §6.4).  Values are part of the
    serialized contract — never rename them."""

    # positive outcomes
    ACCEPTED = "accepted"
    QUEUED = "queued"
    REPLAYED = "replayed"                # ledger idempotency: first ack replayed
    # admission-chain rejections, in chain order
    UNKNOWN_COMMAND = "unknown_command"
    INVALID_PAYLOAD = "invalid_payload"
    COMMAND_ID_CONFLICT = "command_id_conflict"
    MAINTENANCE_MODE = "maintenance_mode"
    PLUGIN_API_INCOMPATIBLE = "plugin_api_incompatible"
    MISSING_ROLE = "missing_role"
    KIND_MISMATCH = "kind_mismatch"
    HEALTH_STALE = "health_stale"
    DEVICE_NOT_READY = "device_not_ready"
    CALIBRATION_EXPIRED = "calibration_expired"   # Phase D+ (profile binding)
    DEVICE_BUSY = "device_busy"
    # built-in (pause/resume/cancel) rejections
    UNKNOWN_TASK = "unknown_task"
    INVALID_STATE = "invalid_state"
    # anything that escaped the chain
    INTERNAL_ERROR = "internal_error"

    @property
    def is_accepted(self) -> bool:
        return self in (AckCode.ACCEPTED, AckCode.QUEUED, AckCode.REPLAYED)


#: The admission chain speaks the same vocabulary as the ack it produces.
AdmissionCode = AckCode


class AdmissionDecision(ContractModel):
    """Outcome of one admission-chain traversal (plan §6.4): a stable code
    plus human detail.  ``task_id`` is set when a run was created/queued."""

    code: AckCode
    detail: str | None = None
    task_id: TaskId | None = None
    error: ErrorInfo | None = None

    @property
    def admitted(self) -> bool:
        return self.code.is_accepted


class CommandAck(ContractModel):
    command_id: str
    accepted: bool
    code: AckCode
    task_id: TaskId | None = None
    queued: bool = False
    reason: str | None = None          # human detail only — never parsed
    error: ErrorInfo | None = None


def ack_from_decision(command_id: str, decision: AdmissionDecision) -> CommandAck:
    return CommandAck(
        command_id=command_id,
        accepted=decision.admitted,
        code=decision.code,
        task_id=decision.task_id,
        queued=decision.code is AckCode.QUEUED,
        reason=decision.detail,
        error=decision.error,
    )
