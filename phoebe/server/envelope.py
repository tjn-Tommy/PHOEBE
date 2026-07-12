"""Response envelope construction (A12): one shape for every /api/v1 body."""
from __future__ import annotations

from typing import Any

from ..contracts.api import ApiEnvelope, ApiError
from ..contracts.commands import AckCode, CommandAck


def ok(data: Any = None) -> dict[str, Any]:
    return ApiEnvelope(status="ok", data=data).model_dump(mode="json")


def warning(data: Any, message: str) -> dict[str, Any]:
    return ApiEnvelope(status="warning", data=data,
                       warning=message).model_dump(mode="json")


def failure(error: ApiError) -> dict[str, Any]:
    return ApiEnvelope(status="error", error=error).model_dump(mode="json")


def ack_envelope(ack: CommandAck) -> dict[str, Any]:
    """A plain ACCEPTED ack is "ok"; anything else — queued, replayed, or any
    typed rejection — is "warning": the call itself succeeded, the outcome
    needs the caller's attention.  Typed clients branch on ``data.code``
    regardless (never on the envelope, never on prose)."""
    data = ack.model_dump(mode="json")
    if ack.code is AckCode.ACCEPTED:
        return ok(data)
    detail = f"{ack.code.value}" + (f": {ack.reason}" if ack.reason else "")
    return warning(data, detail)
