"""Session-token auth + role gate + exposure ladder (plan E-1/E-4).

The ladder (lessons §8.2), enforced **fail-closed at startup**:

* rung 0 (default): loopback bind, per-process generated token, ``operator``
  role — the Tauri/localhost posture.
* rung 1: any non-loopback bind requires an *explicit* token **and**
  ``role = "read_only"``.  Higher rungs (audited restricted submit over the
  network) do not exist yet and therefore cannot be configured.

Auth is header-only (``Authorization: Bearer <token>`` or ``X-Phoebe-Token``)
— tokens never ride query strings, so they never land in access logs.  The
static client uses fetch-streaming for SSE precisely so the header works
there too.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from ..contracts.api import ApiError, ApiErrorCode, ServerRole
from ..contracts.errors import ErrorInfo, PhoebeConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from ..core.config import ServerConfig


class ApiHttpError(Exception):
    """Transport-level failure raised inside routes/dependencies; the global
    handler renders it as the error envelope (A12)."""

    def __init__(self, status: int, code: ApiErrorCode, message: str,
                 info: ErrorInfo | None = None) -> None:
        super().__init__(message)
        self.error = ApiError(status=status, code=code, message=message,
                              info=info)


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ServerSecurity:
    """Resolved security posture for one server process."""

    token: str
    role: ServerRole
    generated: bool = False              # token minted for this process only


def resolve_security(cfg: ServerConfig) -> ServerSecurity:
    """Apply the ladder; raise ``PhoebeConfigError`` rather than bind open."""
    if not is_loopback_host(cfg.host):
        if not cfg.token:
            raise PhoebeConfigError(
                f"server.host = {cfg.host!r} is not loopback and no explicit "
                "server.token is set — refusing to bind (exposure ladder, "
                "lessons §8.2)")
        if cfg.role != "read_only":
            raise PhoebeConfigError(
                "non-localhost binds are read_only-first: set server.role = "
                '"read_only" (network submit is a later ladder rung)')
    if cfg.token:
        return ServerSecurity(token=cfg.token, role=cfg.role)
    return ServerSecurity(token=secrets.token_urlsafe(32), role=cfg.role,
                          generated=True)


def _supplied_token(request: Request) -> str | None:
    scheme, _, param = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and param.strip():
        return param.strip()
    return request.headers.get("x-phoebe-token") or None


def make_require_token(
    security: ServerSecurity,
) -> Callable[[Request], Coroutine[None, None, None]]:
    async def require_token(request: Request) -> None:
        supplied = _supplied_token(request)
        if supplied is None or not hmac.compare_digest(supplied, security.token):
            raise ApiHttpError(401, ApiErrorCode.UNAUTHORIZED,
                               "missing or invalid session token")
    return require_token


def make_require_operator(
    security: ServerSecurity,
) -> Callable[[], Coroutine[None, None, None]]:
    async def require_operator() -> None:
        if security.role != "operator":
            raise ApiHttpError(403, ApiErrorCode.FORBIDDEN,
                               "read_only session: mutating endpoints are disabled")
    return require_operator
