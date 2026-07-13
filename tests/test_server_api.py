"""HTTP adapter over services (plan Phase E, PRs E-1/E-2/E-4).

Exercised entirely offline through httpx's ASGI transport against a full sim
runtime — the same acceptance the plan asks of the web stack: parity with
every PyQt operation, typed envelopes (zero prose parsing), SSE gap repair,
the A14 static version cascade and the fail-closed exposure ladder.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.api import ApiEnvelope, ApiError, ApiErrorCode, ServerMeta
from phoebe.contracts.base import validate_boundary
from phoebe.contracts.errors import PhoebeConfigError
from phoebe.contracts.export import CONTRACTS_VERSION, build_bundle
from phoebe.core.config import ServerConfig, parse_app_config
from phoebe.core.contracts import timestamps
from phoebe.core.events import ProgressEvent, RunState
from phoebe.plugins import load_builtin_plugins
from phoebe.server.app import create_app
from phoebe.server.auth import ServerSecurity, resolve_security
from phoebe.server.routes import UI_PARITY

load_builtin_plugins()

TOKEN = "test-token"
SLM_H, SLM_W = 60, 80


def _sim_config(runs_root: str) -> dict:
    return {
        "mode": "dev",
        "storage": {"runs_root": runs_root},
        "server": {"sse_keepalive_s": 0.05},
        "instruments": [
            {"instrument_id": "slm.primary", "kind": "pattern_modulator",
             "vendor": "santec", "model": "slm-200", "role": "primary_slm",
             "backend": "sim",
             "connection": {"transport": "vendor_dll", "dll_path": "unused"},
             "options": {"settle_ms": 1.0, "height": SLM_H, "width": SLM_W,
                         "levels": 1024, "lut_id": "sim_lut"}},
            {"instrument_id": "osa.main", "kind": "spectrum_analyzer",
             "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
             "backend": "sim",
             "connection": {"transport": "tcp", "host": "sim", "port": 10001}},
        ],
        "plugins": {
            "org.lab.tpa_multiplier": {"bindings": {"slm": "primary_slm",
                                                    "osa": "main_osa"}},
        },
    }


@pytest.fixture()
async def runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


def _make_app(runtime, tmp_path, *, role="operator", static_dir=None):
    return create_app(runtime.services,
                      security=ServerSecurity(token=TOKEN, role=role),
                      state_dir=tmp_path / "state", static_dir=static_dir)


def _client(app, token=TOKEN):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=transport, headers=headers,
                             base_url="http://phoebe.test")


@pytest.fixture()
async def api(runtime, tmp_path):
    app = _make_app(runtime, tmp_path)
    async with _client(app) as client:
        yield client, app


def _tpa_payload(steps: int = 2) -> dict:
    return {"max_steps": steps, "seed": 1,
            "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}


def _envelope_dict(command: str, payload: dict) -> dict:
    return {"command_id": f"cmd-{uuid.uuid4().hex[:8]}", "command": command,
            "payload": payload, "issued_by": "pytest"}


def _progress(step: int) -> ProgressEvent:
    return ProgressEvent(step=step, **timestamps())


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for chunk in text.split("\n\n"):
        frame: dict = {}
        for line in chunk.split("\n"):
            for key in ("id", "event", "data", "retry"):
                if line.startswith(key + ":"):
                    frame[key] = line[len(key) + 1:].strip()
        if frame:
            frames.append(frame)
    return frames


# ------------------------------------------------------------------ E-1 auth
async def test_missing_or_wrong_token_is_401(runtime, tmp_path):
    app = _make_app(runtime, tmp_path)
    async with _client(app, token=None) as anon:
        r = await anon.get("/api/v1/devices")
        assert r.status_code == 401
        body = r.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "unauthorized"
    async with _client(app, token="wrong") as bad:
        assert (await bad.get("/api/v1/devices")).status_code == 401
    async with _client(app) as good:
        assert (await good.get("/api/v1/devices")).status_code == 200


async def test_read_only_role_forbids_mutations(runtime, tmp_path):
    """Exposure ladder rung 1 (E-4): a read_only session can observe
    everything and change nothing."""
    app = _make_app(runtime, tmp_path, role="read_only")
    async with _client(app) as client:
        assert (await client.get("/api/v1/runs")).status_code == 200
        assert (await client.get("/api/v1/devices")).status_code == 200
        r = await client.post("/api/v1/commands",
                              json=_envelope_dict("start_tpa_run", _tpa_payload()))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
        assert (await client.post("/api/v1/devices/health-check")).status_code == 403
        assert (await client.post("/api/v1/runs/t1/cancel")).status_code == 403


def test_exposure_ladder_fails_closed():
    """E-4: a non-loopback bind without explicit token + read_only role must
    refuse to start — misconfiguration can never widen the surface."""
    with pytest.raises(PhoebeConfigError):
        resolve_security(ServerConfig(host="0.0.0.0"))
    with pytest.raises(PhoebeConfigError):        # token set, role still operator
        resolve_security(ServerConfig(host="192.168.1.5", token="secret"))
    sec = resolve_security(ServerConfig(host="192.168.1.5", token="secret",
                                        role="read_only"))
    assert sec.role == "read_only" and not sec.generated
    local = resolve_security(ServerConfig())      # localhost: token generated
    assert local.generated and len(local.token) >= 32


# ------------------------------------------------------------- E-1 envelope
async def test_meta_reports_versions_and_role(api):
    client, _ = api
    data = (await client.get("/api/v1/meta")).json()["data"]
    assert data["api_version"] == 1
    assert data["contracts_version"] == CONTRACTS_VERSION
    assert data["role"] == "operator"
    assert data["static_ui"] == "ok"     # packaged dist pins the current version


async def test_rejected_ack_is_warning_with_typed_code(api):
    """A domain rejection is data, not an ApiError: HTTP 200, envelope
    status=warning, and the client branches on data.code — zero prose."""
    client, _ = api
    r = await client.post("/api/v1/commands",
                          json=_envelope_dict("start_tpa_run",
                                              {"max_steps": "not-an-int"}))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "warning"
    assert body["data"]["accepted"] is False
    assert body["data"]["code"] == "invalid_payload"


async def test_unknown_run_and_unknown_route_are_typed_404(api):
    client, _ = api
    r = await client.get("/api/v1/runs/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = await client.get("/api/v1/warp-drive")
    assert r.status_code == 404
    assert r.json()["status"] == "error"


async def test_malformed_body_is_422(api):
    client, _ = api
    r = await client.post("/api/v1/commands", content=b"{not json",
                          headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation"
    r = await client.post("/api/v1/commands", json={"command": "x"})  # no id
    assert r.status_code == 422


def test_api_contract_round_trips():
    err = ApiError(status=404, code=ApiErrorCode.NOT_FOUND, message="x")
    assert validate_boundary(ApiError, json.loads(err.model_dump_json())) == err
    env = ApiEnvelope(status="warning", data={"a": [1, 2]}, warning="w")
    assert validate_boundary(ApiEnvelope, json.loads(env.model_dump_json())) == env
    meta = ServerMeta(app_version="0.1.0", api_version=1, contracts_version=2,
                      role="operator", current_seq=5, static_ui="ok")
    assert validate_boundary(ServerMeta, json.loads(meta.model_dump_json())) == meta


# --------------------------------------------------------------- E-1 parity
async def test_every_ui_operation_has_a_route(api):
    """Plan E-1 acceptance: every PyQt operation is available over HTTP —
    asserted against the *published* OpenAPI surface, not internals."""
    _, app = api
    paths = app.openapi()["paths"]
    available = {(method.upper(), path)
                 for path, operations in paths.items() for method in operations}
    for operation, (method, path) in UI_PARITY.items():
        assert (method, path) in available, \
            f"missing route for UI operation: {operation}"


async def test_openapi_published_without_token(runtime, tmp_path):
    app = _make_app(runtime, tmp_path)
    async with _client(app, token=None) as anon:
        r = await anon.get("/api/v1/openapi.json")
        assert r.status_code == 200
        assert "/api/v1/commands" in r.json()["paths"]


async def test_schemas_endpoint_serves_live_bundle(api):
    client, _ = api
    data = (await client.get("/api/v1/schemas")).json()["data"]
    assert data == build_bundle()


# ----------------------------------------------------------------- E-1 e2e
async def test_e2e_submit_stream_cancel_catalog(api, runtime):
    """The plan's sim-mode E2E: submit → live preview over the wire →
    cancel → catalog entry + journal, all through /api/v1."""
    client, _ = api
    r = await client.post("/api/v1/commands",
                          json=_envelope_dict("start_tpa_run",
                                              _tpa_payload(10_000)))
    body = r.json()
    assert body["status"] == "ok"
    ack = body["data"]
    assert ack["code"] == "accepted" and ack["task_id"]
    task_id = ack["task_id"]

    stream = asyncio.create_task(client.get(
        "/api/v1/events/stream",
        params={"topics": "data_pointer", "limit": 1}))
    resp = await asyncio.wait_for(stream, 15)
    frames = [f for f in _parse_sse(resp.text) if f.get("event") == "data_pointer"]
    assert frames, "no live preview arrived over SSE"
    preview = json.loads(frames[0]["data"])["preview"]
    assert preview and preview["preview_type"] in (
        "spectrum", "waveform", "image", "scalar_series")

    r = await client.post(f"/api/v1/runs/{task_id}/cancel")
    assert r.json()["data"]["code"] == "accepted"
    assert (await runtime.task_manager.wait(task_id)) is RunState.ABORTED
    await asyncio.sleep(0.2)

    rows = (await client.get("/api/v1/runs")).json()["data"]
    assert len(rows) == 1 and rows[0]["state"] == "aborted"
    records = (await client.get(
        f"/api/v1/runs/{rows[0]['run_id']}/journal")).json()["data"]
    assert records and records[-1]["record"] == "finalized"


async def test_duplicate_command_id_replays_same_task(api, runtime):
    """Ledger semantics over HTTP: a client retry with the same command_id
    can never double-start a run."""
    client, _ = api
    envelope = _envelope_dict("start_tpa_run", _tpa_payload(2))
    first = (await client.post("/api/v1/commands", json=envelope)).json()
    task_id = first["data"]["task_id"]
    await runtime.task_manager.wait(task_id)
    await asyncio.sleep(0.1)

    second = (await client.post("/api/v1/commands", json=envelope)).json()
    assert second["status"] == "warning"
    assert second["data"]["code"] == "replayed"
    assert second["data"]["task_id"] == task_id


async def test_device_actions_over_http(api):
    client, _ = api
    rows = (await client.get("/api/v1/devices")).json()["data"]
    assert {r["instrument_id"] for r in rows} == {"slm.primary", "osa.main"}
    assert all(r["lifecycle"] == "ready" for r in rows)
    assert (await client.post(
        "/api/v1/devices/slm.primary/reconnect")).json()["data"] is True
    stats = (await client.get("/api/v1/devices/stats")).json()["data"]
    assert set(stats) == {"slm.primary", "osa.main"}


async def test_plugin_platform_over_http(api):
    """D-1 surface: availability report + enable/disable with typed acks."""
    client, _ = api
    rows = (await client.get("/api/v1/plugins")).json()["data"]
    states = {r["plugin_id"]: r["state"] for r in rows}
    assert states.get("org.lab.tpa_multiplier") == "loaded"

    try:
        r = await client.post("/api/v1/plugins/org.lab.tpa_multiplier/disable")
        assert r.json()["status"] == "ok"
        submit = (await client.post(
            "/api/v1/commands",
            json=_envelope_dict("start_tpa_run", _tpa_payload(2)))).json()
        assert submit["status"] == "warning"
        assert submit["data"]["code"] == "plugin_disabled"
        rows = (await client.get("/api/v1/plugins")).json()["data"]
        states = {r["plugin_id"]: r["state"] for r in rows}
        assert states["org.lab.tpa_multiplier"] == "disabled"
        assert (await client.post(
            "/api/v1/plugins/org.nope/enable")).status_code == 404
    finally:
        r = await client.post("/api/v1/plugins/org.lab.tpa_multiplier/enable")
        assert r.json()["status"] == "ok"


async def test_plugin_schema_over_http(api):
    client, _ = api
    commands = (await client.get("/api/v1/plugins/commands")).json()["data"]
    assert "start_tpa_run" in commands
    schema = (await client.get(
        "/api/v1/plugins/commands/start_tpa_run/schema")).json()["data"]
    assert "max_steps" in schema["properties"]
    assert (await client.get(
        "/api/v1/plugins/commands/warp/schema")).status_code == 404


# ----------------------------------------------------------------- E-2 SSE
async def test_event_snapshot_and_replay_endpoints(api, runtime):
    client, _ = api
    ack = (await client.post(
        "/api/v1/commands",
        json=_envelope_dict("start_tpa_run", _tpa_payload(2)))).json()["data"]
    await runtime.task_manager.wait(ack["task_id"])
    await asyncio.sleep(0.2)

    snap = (await client.get("/api/v1/events/snapshot",
                             params={"topics": "run_state"})).json()["data"]
    assert snap["events"] and snap["events"][-1]["final"] is True
    assert snap["current_seq"] > 0
    replayed = (await client.get(
        "/api/v1/events/replay",
        params={"since_seq": 0, "topics": "run_state"})).json()["data"]
    assert replayed and all(e["event_type"] == "run_state" for e in replayed)
    stats = (await client.get("/api/v1/events/stats")).json()["data"]
    assert stats["current_seq"] >= snap["current_seq"]


async def test_sse_gap_repair_since_seq(api, runtime):
    """Plan E-2 acceptance: a reconnecting client passes its cursor and
    receives exactly the missed events (from the ring), then live ones."""
    client, _ = api
    bus = runtime.bus
    for i in range(3):
        bus.publish(_progress(i))
    cutoff = bus.current_seq
    bus.publish(_progress(3))
    bus.publish(_progress(4))

    stream = asyncio.create_task(client.get(
        "/api/v1/events/stream",
        params={"topics": "progress", "since_seq": cutoff, "limit": 3}))
    await asyncio.sleep(0.1)
    bus.publish(_progress(5))                     # live event after connect
    resp = await asyncio.wait_for(stream, 10)

    frames = [f for f in _parse_sse(resp.text) if f.get("event") == "progress"]
    steps = [json.loads(f["data"])["step"] for f in frames]
    assert steps == [3, 4, 5]
    seqs = [int(f["id"]) for f in frames]
    assert seqs == sorted(seqs) and seqs[0] > cutoff


async def test_sse_last_event_id_header(api, runtime):
    """The standard EventSource reconnect header is honored as the cursor."""
    client, _ = api
    bus = runtime.bus
    bus.publish(_progress(0))
    cutoff = bus.current_seq
    bus.publish(_progress(1))
    bus.publish(_progress(2))

    resp = await asyncio.wait_for(asyncio.create_task(client.get(
        "/api/v1/events/stream",
        params={"topics": "progress", "limit": 2},
        headers={"last-event-id": str(cutoff)})), 10)
    frames = [f for f in _parse_sse(resp.text) if f.get("event") == "progress"]
    assert [json.loads(f["data"])["step"] for f in frames] == [1, 2]


async def test_sse_keepalive_comment(api, runtime):
    """Idle streams stay alive via comment pings (interval from config)."""
    client, _ = api
    stream = asyncio.create_task(client.get(
        "/api/v1/events/stream", params={"topics": "progress", "limit": 1}))
    await asyncio.sleep(0.2)                      # > sse_keepalive_s = 0.05
    runtime.bus.publish(_progress(99))
    resp = await asyncio.wait_for(stream, 10)
    assert ": keepalive" in resp.text
    assert "retry:" in resp.text


# --------------------------------------------------------- E-4 audit trail
async def test_mutations_are_audited(runtime, tmp_path):
    app = _make_app(runtime, tmp_path)
    async with _client(app) as client:
        envelope = _envelope_dict("start_tpa_run", _tpa_payload(2))
        ack = (await client.post("/api/v1/commands", json=envelope)).json()["data"]
        await runtime.task_manager.wait(ack["task_id"])
        await client.post("/api/v1/devices/health-check")
        await client.get("/api/v1/runs")          # reads are not audited

    lines = (tmp_path / "state" / "audit.jsonl").read_text("utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert [e["action"] for e in entries] == ["submit", "health_check"]
    assert entries[0]["target"] == "start_tpa_run"
    assert entries[0]["outcome"] == "accepted"
    assert all(e["actor"] and e["t_wall"] for e in entries)


# ------------------------------------------------------- E-3 static cascade
def _static_dist(tmp_path, version):
    root = tmp_path / f"dist-{version}"
    root.mkdir()
    (root / "index.html").write_text("<h1>phoebe-ui</h1>", encoding="utf-8")
    if version is not None:
        (root / "version").write_text(str(version), encoding="utf-8")
    return root


async def test_static_version_cascade(runtime, tmp_path):
    """A14: matching pin serves; older serves flagged; newer/unpinned is
    refused; absent means API-only.  Static assets need no token."""
    app = _make_app(runtime, tmp_path,
                    static_dir=_static_dist(tmp_path, CONTRACTS_VERSION))
    async with _client(app, token=None) as anon:
        r = await anon.get("/ui/")
        assert r.status_code == 200 and "phoebe-ui" in r.text
        r = await anon.get("/", follow_redirects=False)
        assert r.status_code in (302, 307)
    async with _client(app) as client:
        assert (await client.get("/api/v1/meta")).json()["data"]["static_ui"] == "ok"

    app = _make_app(runtime, tmp_path,
                    static_dir=_static_dist(tmp_path, CONTRACTS_VERSION - 1))
    async with _client(app) as client:
        assert (await client.get("/ui/")).status_code == 200
        meta = (await client.get("/api/v1/meta")).json()["data"]
        assert meta["static_ui"] == "outdated"

    app = _make_app(runtime, tmp_path,
                    static_dir=_static_dist(tmp_path, CONTRACTS_VERSION + 1))
    async with _client(app) as client:
        r = await client.get("/ui/")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "unavailable"
        meta = (await client.get("/api/v1/meta")).json()["data"]
        assert meta["static_ui"] == "refused"

    app = _make_app(runtime, tmp_path, static_dir=tmp_path / "missing")
    async with _client(app) as client:
        r = await client.get("/")
        assert r.json()["static_ui"] == "absent"


# ------------------------------------------------------------------ E-3 CORS
async def test_cors_allowlist_for_desktop_origins(api):
    """The Tauri desktop client and the vite dev server are separate origins;
    they get a CORS grant.  Foreign origins get none (auth stays header-token
    based with no cookies, so this cannot leak an authenticated session)."""
    client, _ = api
    desktop = {"origin": "http://tauri.localhost"}
    pre = await client.options("/api/v1/commands", headers={
        **desktop,
        "access-control-request-method": "POST",
        "access-control-request-headers": "authorization,content-type"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert "POST" in pre.headers["access-control-allow-methods"]

    res = await client.get("/api/v1/meta", headers=desktop)
    assert res.headers["access-control-allow-origin"] == "http://tauri.localhost"

    foreign = {"origin": "https://evil.example"}
    res = await client.get("/api/v1/meta", headers=foreign)
    assert "access-control-allow-origin" not in res.headers
    pre = await client.options("/api/v1/commands", headers={
        **foreign, "access-control-request-method": "POST"})
    assert pre.status_code == 400
