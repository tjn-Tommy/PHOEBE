"""/api/v1 route surface (plan E-1): parity with every PyQt operation.

Each route is a thin bridge: parse/validate at the boundary → one service
call marshalled onto the core loop (``ServiceHub.call`` + ``wrap_future``) →
envelope out.  No route touches a controller, driver, transport, or raw
SCPI — the import-linter server contract forbids it, same as the UI.

Mutating routes (POST) carry the operator-role gate and write the audit log.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import ValidationError

from ..contracts.api import API_VERSION, ApiErrorCode, ServerMeta
from ..contracts.base import validate_boundary
from ..contracts.commands import CommandEnvelope
from ..contracts.errors import error_info_of
from ..contracts.export import CONTRACTS_VERSION, build_bundle
from ..services.events import DEFAULT_TOPICS
from .auth import ApiHttpError, make_require_operator, make_require_token
from .envelope import ack_envelope, ok
from .sse import stream_events

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..services import ServiceHub
    from .audit import AuditLog
    from .auth import ServerSecurity
    from .static import StaticStatus

#: One place that says which UI operation each route covers — the parity
#: test in tests/test_server_api.py walks this table against the app.
UI_PARITY: dict[str, tuple[str, str]] = {
    "submit command":        ("POST", "/api/v1/commands"),
    "pause run":             ("POST", "/api/v1/runs/{task_id}/pause"),
    "resume run":            ("POST", "/api/v1/runs/{task_id}/resume"),
    "cancel run":            ("POST", "/api/v1/runs/{task_id}/cancel"),
    "runs panel (catalog)":  ("GET", "/api/v1/runs"),
    "run detail":            ("GET", "/api/v1/runs/{run_id}"),
    "run journal":           ("GET", "/api/v1/runs/{run_id}/journal"),
    "active tasks":          ("GET", "/api/v1/tasks"),
    "device table":          ("GET", "/api/v1/devices"),
    "device stats":          ("GET", "/api/v1/devices/stats"),
    "device reconnect":      ("POST", "/api/v1/devices/{instrument_id}/reconnect"),
    "device disable":        ("POST", "/api/v1/devices/{instrument_id}/disable"),
    "health check all":      ("POST", "/api/v1/devices/health-check"),
    "plugin command list":   ("GET", "/api/v1/plugins/commands"),
    "plugin config schema":  ("GET", "/api/v1/plugins/commands/{command}/schema"),
    "instrument config":     ("GET", "/api/v1/config/instruments"),
    "event snapshot":        ("GET", "/api/v1/events/snapshot"),
    "event replay":          ("GET", "/api/v1/events/replay"),
    "bus stats":             ("GET", "/api/v1/events/stats"),
    "live event stream":     ("GET", "/api/v1/events/stream"),
    "contracts schema":      ("GET", "/api/v1/schemas"),
    "server meta":           ("GET", "/api/v1/meta"),
}


def _topics_of(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return tuple(DEFAULT_TOPICS)
    topics = tuple(t.strip() for t in raw.split(",") if t.strip())
    if not topics:
        raise ApiHttpError(422, ApiErrorCode.VALIDATION, "empty topics filter")
    return topics


def _actor(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def build_router(
    services: ServiceHub,
    *,
    security: ServerSecurity,
    audit: AuditLog,
    app_version: str,
    static_ui: StaticStatus,
    sse_keepalive_s: float,
) -> APIRouter:
    api = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(make_require_token(security))],
    )
    write = [Depends(make_require_operator(security))]
    schema_bundle = build_bundle()       # pure function of the contracts

    async def call(coro: Coroutine[Any, Any, Any]) -> Any:
        """Marshal one service coroutine onto the core loop; anything that
        escapes becomes a typed 500 envelope (A12 global handler)."""
        try:
            return await asyncio.wrap_future(services.call(coro))
        except (ApiHttpError, asyncio.CancelledError):
            raise
        except KeyError as exc:
            raise ApiHttpError(404, ApiErrorCode.NOT_FOUND,
                               f"unknown resource: {exc}") from exc
        except Exception as exc:
            logger.exception("API service call failed")
            raise ApiHttpError(500, ApiErrorCode.INTERNAL,
                               f"{type(exc).__name__}: {exc}",
                               info=error_info_of(exc)) from exc

    # ---------------------------------------------------------------- meta
    @api.get("/meta")
    async def meta() -> dict:
        return ok(ServerMeta(
            app_version=app_version, api_version=API_VERSION,
            contracts_version=CONTRACTS_VERSION, role=security.role,
            current_seq=services.events.current_seq,
            static_ui=static_ui).model_dump(mode="json"))

    @api.get("/schemas")
    async def schemas() -> dict:
        return ok(schema_bundle)

    # ------------------------------------------------------------ commands
    @api.post("/commands", dependencies=write)
    async def submit_command(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise ApiHttpError(422, ApiErrorCode.VALIDATION,
                               f"body is not valid JSON: {exc}") from exc
        try:
            envelope = validate_boundary(CommandEnvelope, body)
        except ValidationError as exc:
            raise ApiHttpError(422, ApiErrorCode.VALIDATION,
                               f"invalid CommandEnvelope: {exc}") from exc
        ack = await call(services.runs.submit(envelope))
        audit.record(actor=_actor(request), action="submit",
                     target=envelope.command, outcome=ack.code.value)
        return ack_envelope(ack)

    def _run_control(action: str):
        async def control(task_id: str, request: Request) -> dict:
            ack = await call(getattr(services.runs, action)(task_id))
            audit.record(actor=_actor(request), action=action,
                         target=task_id, outcome=ack.code.value)
            return ack_envelope(ack)
        control.__name__ = f"{action}_run"
        return control

    api.post("/runs/{task_id}/pause", dependencies=write)(_run_control("pause"))
    api.post("/runs/{task_id}/resume", dependencies=write)(_run_control("resume"))
    api.post("/runs/{task_id}/cancel", dependencies=write)(_run_control("cancel"))

    # ---------------------------------------------------------------- runs
    @api.get("/runs")
    async def list_runs(limit: int = 100, offset: int = 0) -> dict:
        rows = await call(services.runs.list_runs(limit=limit, offset=offset))
        return ok([r.model_dump(mode="json") for r in rows])

    @api.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        row = await call(services.runs.get_run(run_id))
        if row is None:
            raise ApiHttpError(404, ApiErrorCode.NOT_FOUND,
                               f"unknown run {run_id!r}")
        return ok(row.model_dump(mode="json"))

    @api.get("/runs/{run_id}/journal")
    async def run_journal(run_id: str) -> dict:
        if await call(services.runs.get_run(run_id)) is None:
            raise ApiHttpError(404, ApiErrorCode.NOT_FOUND,
                               f"unknown run {run_id!r}")
        records = await call(services.runs.read_run_journal(run_id))
        return ok([r.model_dump(mode="json") for r in records])

    @api.get("/tasks")
    async def active_tasks() -> dict:
        tasks = await call(services.runs.active_tasks())
        return ok([str(t) for t in tasks])

    # ------------------------------------------------------------- devices
    @api.get("/devices")
    async def device_table() -> dict:
        rows = await call(services.devices.table())
        return ok([r.model_dump(mode="json") for r in rows])

    @api.get("/devices/stats")
    async def device_stats() -> dict:
        stats = await call(services.devices.stats())
        return ok({k: v.model_dump(mode="json") for k, v in stats.items()})

    @api.post("/devices/health-check", dependencies=write)
    async def health_check(request: Request) -> dict:
        await call(services.devices.health_check_all())
        audit.record(actor=_actor(request), action="health_check", outcome="ok")
        return ok()

    @api.post("/devices/{instrument_id}/reconnect", dependencies=write)
    async def reconnect(instrument_id: str, request: Request) -> dict:
        result = await call(services.devices.reconnect(instrument_id))
        audit.record(actor=_actor(request), action="reconnect",
                     target=instrument_id, outcome=str(bool(result)).lower())
        return ok(bool(result))

    @api.post("/devices/{instrument_id}/disable", dependencies=write)
    async def disable(instrument_id: str, request: Request) -> dict:
        await call(services.devices.disable(instrument_id))
        audit.record(actor=_actor(request), action="disable",
                     target=instrument_id, outcome="ok")
        return ok()

    # ------------------------------------------------------------- plugins
    @api.get("/plugins/commands")
    async def plugin_commands() -> dict:
        return ok(list(await call(services.plugins.commands())))

    @api.get("/plugins/commands/{command}/schema")
    async def plugin_schema(command: str) -> dict:
        schema = await call(services.plugins.config_schema(command))
        if schema is None:
            raise ApiHttpError(404, ApiErrorCode.NOT_FOUND,
                               f"unknown command {command!r}")
        return ok(schema)

    # -------------------------------------------------------------- config
    @api.get("/config/instruments")
    async def instrument_config() -> dict:
        instruments = await call(services.config.instruments())
        return ok([c.model_dump(mode="json") for c in instruments])

    # -------------------------------------------------------------- events
    @api.get("/events/snapshot")
    async def event_snapshot(topics: str | None = None) -> dict:
        events = await call(services.events.snapshot(_topics_of(topics)))
        return ok({"events": [e.model_dump(mode="json") for e in events],
                   "current_seq": services.events.current_seq})

    @api.get("/events/replay")
    async def event_replay(since_seq: int, topics: str | None = None) -> dict:
        selected = None if topics is None else _topics_of(topics)
        events = await call(services.events.replay_since(since_seq, selected))
        return ok([e.model_dump(mode="json") for e in events])

    @api.get("/events/stats")
    async def event_stats() -> dict:
        stats = await call(services.events.bus_stats())
        return ok(stats.model_dump(mode="json"))

    @api.get("/events/stream")
    async def event_stream(request: Request, topics: str | None = None,
                           since_seq: int | None = None,
                           limit: int | None = None) -> StreamingResponse:
        if since_seq is None:
            last_id = request.headers.get("last-event-id")
            if last_id is not None:
                try:
                    since_seq = int(last_id)
                except ValueError as exc:
                    raise ApiHttpError(
                        422, ApiErrorCode.VALIDATION,
                        "Last-Event-ID must be an integer seq") from exc
        generator = stream_events(
            services, topics=_topics_of(topics), since_seq=since_seq,
            keepalive_s=sse_keepalive_s, limit=limit)
        return StreamingResponse(
            generator, media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    return api
