# PHOEBE HTTP adapter — threat model & exposure ladder (Phase E-4)

Status: satisfied for the **localhost rung**; higher rungs gated below.
Verified against `phoebe/server/` and `tests/test_server_api.py` (every claim
here is enforced by code or a test — the standing "provable from code" rule).

## 1. What the server is

A FastAPI adapter over `phoebe/services/` — the same `ServiceHub` the PyQt
shell uses in-process. It holds no privileged path of its own:

- Commands enter only as `CommandEnvelope` → `Gateway` → admission chain.
  There is **no raw-SCPI, no arbitrary-Python, no Controller/Driver endpoint**;
  the import-linter contract *"Server stays on the services surface"*
  (pyproject.toml) makes a reach-around a CI failure, same as for the UI.
- Every response is the `ApiEnvelope` (A12); domain rejections are typed
  `AckCode`s in data — clients parse zero prose.

## 2. Trigger conditions (lessons §8.2) — checklist

| Condition | Status |
|---|---|
| Need for multi-user / cross-machine / headless access | **Not yet claimed** — default posture is localhost-only (Tauri/browser on the bench PC) |
| Identity, roles, audit, maintenance mode, network isolation | Session token (header-only) · `read_only`/`operator` roles · `audit.jsonl` for every mutation · maintenance gate already in admission · isolation = loopback bind |
| S1/S2/O1 (typed codes, persisted run truth, admission reuse) | Done in Phase C; the remote layer reuses the same admission/lease/journal/services |
| Read-only vs control boundary + threat model | Roles enforced per-route (403 test); this document |

## 3. The ladder (enforced fail-closed at startup)

`phoebe.server.auth.resolve_security` refuses to start rather than bind open
(`test_exposure_ladder_fails_closed`):

| Rung | Bind | Requirements | Status |
|---|---|---|---|
| 0 | loopback only | token auto-generated per process, printed to stdout (never logged — the log bridge broadcasts on the bus); role `operator` allowed | **implemented, default** |
| 1 | non-loopback | explicit `server.token` **and** `server.role = "read_only"` — configs that ask for network + write refuse to start | **implemented** |
| 2 | non-loopback restricted submit | per-user identity, TLS/reverse-proxy guidance, audit review procedure, command allow-list | **not implemented — deliberately unrepresentable in config** |

## 4. Assets and threats considered

- **Hardware safety** (the crown jewel): a network peer must never drive
  instruments. Mitigated by rung 1 forcing read_only, the operator-role gate
  on every POST, and no endpoint below the Gateway.
- **Session token**: header-only (`Authorization: Bearer` / `X-Phoebe-Token`),
  never in query strings → never in access logs; constant-time compare;
  static client keeps it in `sessionStorage` and uses fetch-SSE so the header
  works for streams too.
- **Run data**: read endpoints expose catalog/journal metadata and bounded
  previews only; bulk arrays stay in HDF5 (no dataset endpoint yet — when
  added it must remain post-run, read-only, traversal-safe).
- **UI supply chain**: static dist is version-pinned (A14 cascade: match /
  serve-outdated-with-warning / refuse-newer); serving is Starlette
  `StaticFiles` (traversal-safe); assets carry no secrets.
- **Denial of service / backpressure**: per-subscription bounded queues;
  an overflowing SSE consumer is detached (`stream_reset`) and repairs its
  own gap via `Last-Event-ID` — the publisher and other clients are never
  blocked.
- **Auditability**: every mutating call appends actor/action/target/outcome
  to `runs/.phoebe/audit.jsonl` (`test_mutations_are_audited`); audit failure
  never breaks requests but is logged loudly.

## 5. Explicitly out of scope / never to be added

Raw SCPI passthrough · arbitrary code execution · endpoints that bypass the
Gateway or leases · auto-resume of real runs over the API · token in URLs ·
non-localhost binds without climbing rung 2 first (TLS, identity, review).

## Addendum (desktop client): CORS allowlist

The Tauri desktop client (`desktop/`) and its vite dev server run on
different browser origins than the API (`tauri://localhost`,
`http(s)://tauri.localhost`, `http://localhost:1420`), so
`phoebe.server.app.DESKTOP_ORIGINS` grants exactly those origins CORS access
(`test_cors_allowlist_for_desktop_origins`). This does **not** move the
server up the ladder:

- Authentication stays header-token based; there are no cookies and
  `allow_credentials` stays off, so a hostile page gaining a CORS grant would
  still hold no credential — and hostile pages get no grant: the allowlist is
  fixed at build time, never `*`, and never configurable upward from TOML.
- The SSE stream and every mutating route still require the same token; the
  browser preflight (`OPTIONS`) is unauthenticated by HTTP design but
  performs no work and reveals only the allowlist itself.
