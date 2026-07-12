"""HTTP API wire contracts (plan §6.7, Phase E; lessons A12/A14).

The FastAPI adapter (``phoebe/server/``) is a thin transport over the
services layer — these are the only shapes it adds on top of the existing
contracts: a three-state response envelope, a *transport-level* error, and
the server meta document used for version pinning.  Domain outcomes keep
riding ``CommandAck``/``ErrorInfo`` unchanged: a rejected command is **data**
inside a successful HTTP response, never an ``ApiError``.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from .base import ContractModel
from .errors import ErrorInfo

#: ``/api/v{N}`` — bump only on breaking route/envelope changes.
API_VERSION = 1

#: Exposure-ladder rungs available today (lessons §8.2): ``operator`` is the
#: localhost default; ``read_only`` is the only role a non-localhost bind may
#: use.  Restricted network submit is a later rung — deliberately
#: unrepresentable until it exists.
ServerRole = Literal["read_only", "operator"]


class ApiErrorCode(StrEnum):
    """Transport-level failure vocabulary.  Values are part of the serialized
    contract — never rename them."""

    UNAUTHORIZED = "unauthorized"        # missing/invalid session token
    FORBIDDEN = "forbidden"              # role does not permit the operation
    NOT_FOUND = "not_found"
    VALIDATION = "validation"            # malformed body / query parameters
    UNAVAILABLE = "unavailable"          # e.g. static UI refused by version pin
    INTERNAL = "internal"


class ApiError(ContractModel):
    """One transport-level failure: HTTP status mirror + stable code +
    human detail (never parsed).  ``info`` attributes device/kernel errors
    when one caused the failure."""

    status: int
    code: ApiErrorCode
    message: str
    info: ErrorInfo | None = None


#: Envelope status (A12): ``warning`` = the call succeeded but the outcome
#: needs attention (a non-plain-ACCEPTED ack, a degraded run, ...).
ApiStatus = Literal["ok", "warning", "error"]


class ApiEnvelope(ContractModel):
    """Every ``/api/v1`` response body.  Clients branch on ``status`` for
    coarse UI (toast/banner) and on the typed payload inside ``data`` for
    logic — the envelope itself carries zero domain semantics."""

    status: ApiStatus
    data: Any = None
    warning: str | None = None           # human detail for status="warning"
    error: ApiError | None = None        # set exactly when status="error"


class ServerMeta(ContractModel):
    """``GET /api/v1/meta`` — capability discovery + version pinning (A14).

    ``static_ui`` reports the dist cascade: ``ok`` (pinned versions match),
    ``outdated`` (older dist served with a warning), ``refused`` (newer than
    the backend or unreadable pin — not served), ``absent``.
    """

    name: Literal["phoebe"] = "phoebe"
    app_version: str
    api_version: int
    contracts_version: int
    role: ServerRole
    current_seq: int
    static_ui: Literal["ok", "outdated", "refused", "absent"]
