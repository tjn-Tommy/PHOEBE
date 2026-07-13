"""FastAPI application factory (plan E-1) + global error handlers (A12).

``create_app`` is pure wiring: it never builds a runtime, binds a socket, or
touches a device — it takes an existing ``ServiceHub`` (the same object the
PyQt shell uses) and returns the ASGI app.  ``python -m phoebe.server`` does
the hosting.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..contracts.api import ApiError, ApiErrorCode
from .audit import AUDIT_FILENAME, AuditLog
from .auth import ApiHttpError, ServerSecurity, resolve_security
from .envelope import failure
from .routes import build_router
from .static import DEFAULT_STATIC_DIR, mount_static

if TYPE_CHECKING:
    from ..services import ServiceHub

#: Origins allowed to call /api/v1 from a browser context (E-3 clients).
#: The desktop (Tauri) webview and the vite dev server are different origins
#: than the API, so they need CORS; the allowlist is fixed and loopback-only.
#: This does not weaken the ladder: auth stays header-token based (no
#: cookies, allow_credentials stays False), so a foreign page still cannot
#: authenticate — CORS here only lets the sanctioned local clients read
#: their own authenticated responses.
DESKTOP_ORIGINS = (
    "tauri://localhost",           # Tauri webview (macOS/Linux)
    "http://tauri.localhost",      # Tauri webview (Windows WebView2)
    "https://tauri.localhost",
    "http://localhost:1420",       # `pnpm dev` in desktop/
    "http://127.0.0.1:1420",
)


def _app_version() -> str:
    try:
        return metadata.version("phoebe")
    except metadata.PackageNotFoundError:
        return "unknown"


def _install_error_handlers(app: FastAPI) -> None:
    """Every failure leaves as the same envelope — no bare FastAPI bodies."""

    @app.exception_handler(ApiHttpError)
    async def _api_error(request: Request, exc: ApiHttpError) -> JSONResponse:
        return JSONResponse(status_code=exc.error.status,
                            content=failure(exc.error))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request,
                          exc: RequestValidationError) -> JSONResponse:
        error = ApiError(status=422, code=ApiErrorCode.VALIDATION,
                         message=str(exc))
        return JSONResponse(status_code=422, content=failure(error))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request,
                          exc: StarletteHTTPException) -> JSONResponse:
        code = (ApiErrorCode.NOT_FOUND if exc.status_code == 404
                else ApiErrorCode.INTERNAL)
        error = ApiError(status=exc.status_code, code=code,
                         message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code,
                            content=failure(error))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.opt(exception=exc).error("unhandled error in {} {}",
                                        request.method, request.url.path)
        error = ApiError(status=500, code=ApiErrorCode.INTERNAL,
                         message=f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content=failure(error))


def create_app(
    services: ServiceHub,
    *,
    security: ServerSecurity | None = None,
    audit: AuditLog | None = None,
    static_dir: Path | None = None,
    state_dir: Path | None = None,
) -> FastAPI:
    config = services.config.app_config
    if security is None:
        security = resolve_security(config.server)
    if audit is None:
        base = state_dir or Path(config.storage.runs_root) / ".phoebe"
        audit = AuditLog(base / AUDIT_FILENAME)

    app = FastAPI(
        title="PHOEBE",
        version=_app_version(),
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    _install_error_handlers(app)

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DESKTOP_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-phoebe-token",
                       "last-event-id"],
        max_age=600,
    )

    static_ui = mount_static(app, static_dir or DEFAULT_STATIC_DIR)
    app.include_router(build_router(
        services, security=security, audit=audit,
        app_version=_app_version(), static_ui=static_ui,
        sse_keepalive_s=config.server.sse_keepalive_s,
    ))

    app.state.security = security
    app.state.audit = audit
    return app
