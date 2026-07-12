"""Static UI serving with the A14 version-pinning cascade.

The dist directory carries a ``version`` file holding the *contracts*
version it was built against (the schema bundle's ``contracts_version``):

* equal to the backend        → serve (``ok``)
* older than the backend      → serve, flagged ``outdated`` in ``/meta`` and a
  startup warning — the client shows a banner (fallback-with-warning)
* newer, or pin unreadable    → **refuse** to serve (``refused``): a UI built
  against contracts this backend does not speak yet must not drive it
* directory missing           → ``absent`` (API-only deployment)

Serving itself is Starlette's ``StaticFiles`` — traversal-safe by
construction.  Static assets carry no secrets and are served without the
session token; every API call the page makes still requires it.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger

from ..contracts.api import ApiError, ApiErrorCode
from ..contracts.export import CONTRACTS_VERSION
from .envelope import failure

if TYPE_CHECKING:
    from fastapi import FastAPI

STATIC_VERSION_FILENAME = "version"
DEFAULT_STATIC_DIR = Path(__file__).parent / "static"

StaticStatus = Literal["ok", "outdated", "refused", "absent"]


def static_status(root: Path) -> StaticStatus:
    if not root.is_dir():
        return "absent"
    try:
        pinned = int((root / STATIC_VERSION_FILENAME)
                     .read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "refused"                 # unpinned dist: fail closed
    if pinned == CONTRACTS_VERSION:
        return "ok"
    if pinned < CONTRACTS_VERSION:
        return "outdated"
    return "refused"                     # dist newer than this backend


def mount_static(app: FastAPI, root: Path) -> StaticStatus:
    status = static_status(root)
    if status in ("ok", "outdated"):
        from fastapi.staticfiles import StaticFiles
        app.mount("/ui", StaticFiles(directory=str(root), html=True), name="ui")
        if status == "outdated":
            logger.warning("static UI at {} pins an older contracts version — "
                           "serving with a warning flag", root)
    else:
        if status == "refused":
            logger.warning("static UI at {} refused: version pin missing or "
                           "newer than contracts v{}", root, CONTRACTS_VERSION)

        @app.get("/ui/{_path:path}", include_in_schema=False)
        async def _refused_ui(_path: str) -> JSONResponse:
            error = ApiError(
                status=503, code=ApiErrorCode.UNAVAILABLE,
                message=(f"static UI {status}: rebuild it against contracts "
                         f"v{CONTRACTS_VERSION} (A14 version pin)"))
            return JSONResponse(status_code=503, content=failure(error))

    serving = status in ("ok", "outdated")

    @app.get("/", include_in_schema=False, response_model=None)
    async def _index():
        if serving:
            return RedirectResponse("/ui/")
        return JSONResponse({"name": "phoebe", "api": "/api/v1",
                             "static_ui": status})

    return status
