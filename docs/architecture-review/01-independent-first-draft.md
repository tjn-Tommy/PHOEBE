# PHOEBE Architecture Review — Independent First Draft

> **Provenance note.** This draft was written from code analysis only — the full Phoebe
> codebase (spec `refactor.md` + all `phoebe/` sources + tests), and the AstrBot codebase
> (core lifecycle, star/plugin system, pipeline, platform/provider adapters, config,
> dashboard). It was deliberately written **before** reading `ASTRBOT_DESIGN_LESSONS.md`,
> so its conclusions are independent of that document. The cross-referenced final plan
> lives in `PHOEBE_EVOLUTION_PLAN.md`.

---

## 0. Core judgments (with rationale)

**J1 — Phoebe's architecture is fundamentally sound. This is a completion-and-hardening
program, not a restructuring.** The v2 spec's load-bearing decisions — control/data plane
separation, strict `ContractModel` boundaries, lease ownership table, checkpoint-unified
pause/cancel/heartbeat, three-tier threading with per-device workers — are all *actually
implemented and enforced at runtime* (bus 64KB assert `bus.py:121-126`, choke-point
`validate_boundary` at `config.py:128` / `task_manager.py:204` / `capability.py:125`,
lease atomicity `device_manager.py:123-165`). The failure pattern across every subsystem
report is identical: **happy paths are excellent; failure/recovery paths are incomplete;
and there is no service/query surface**. Evolution should preserve the core and build
the missing edges.

**J2 — The deepest gap versus Phoebe's own requirements is fault recovery and lifecycle,
not modularity.** Evidence cluster: writer-task death permanently hangs a run with no
cancel path (`writer.py:151-156` + checkpoint-only cancellation); the lease reaper
half-recovers and can corrupt the ownership table via unconditional release
(`device_manager.py:167-172, 201-221`); a paused run stops heartbeating and gets reaped
(`task_manager.py:107-114` vs TTL default 600 s); any pre-RUNNING failure hits an illegal
`QUEUED→FAILED` transition and produces a zombie run (`task_manager.py:44-55, 348`);
app shutdown never drains active runs (`bootstrap.py:36-39`, `ui/app.py:54-58`); no
reconnection exists anywhere (`connect_instrument` called only from startup); startup is
all-or-nothing. For a platform whose stated purpose is overnight optimization runs on
real hardware, these are the first things to fix — before any new feature.

**J3 — The frontend migration should be executed as "build the application-service layer
now; make PyQt its first client".** What a Vue/Tauri frontend needs and cannot get today:
a query surface (list commands/plugins/schemas/devices/runs/current state — all of these
are in-process Python calls or config objects today, `main_window.py:51-63`,
`run_sim_demo.py:47`); machine-readable ack/error codes instead of prose
(`"423 locked by ..."` string, `reason == "final"` magic gate `main_window.py:336`);
per-entity retained events + gap-repair replay (today: one retained event per coarse
topic, `bus.py:128`); an actually-exercised serialization boundary (the `GatewayEvent`
union has zero non-test consumers; no `model_json_schema()` call exists in the tree).
Building this service layer *now* — with the PyQt shell refactored to consume it
in-process — means the Tauri migration later only swaps the transport, and no work is
done twice. This is also exactly the shape AstrBot's dashboard validates at scale
(thin FastAPI routes → service layer → core; uniform envelope; SSE with replay).

**J4 — The plugin system needs metadata, lifecycle, and a façade — not AstrBot's
dynamism.** Phoebe's registration core (`@register`/`@on_command` → `PluginSpec`,
dispatch-time DI with fail-fast ambiguity errors) is *better* than AstrBot's
(mutate-the-registry `functools.partial` binding, broadcast filter scans). What it lacks
is everything around the edges: no discovery (hand-edited import list), no plugin
manifest (name/version/author/compat range), no enable/disable, no per-plugin failure
records (one broken plugin import breaks startup), no stable `phoebe.api` façade
(plugins import core paths directly), no plugin-config schema surfaced to any UI. Adopt
AstrBot's edges; keep Phoebe's core.

**J5 — The business models differ fundamentally; adopt AstrBot's boundary engineering,
reject its dispatch semantics.** AstrBot: events are small, droppable, independent;
handlers near-stateless; backends fungible; worst-case failure = a bad chat message.
Hence: unbounded task-per-event concurrency, log-and-continue error policy, mutable
god-event with untyped `_extras`, config as live mutable dict, restart via `os.execv`.
Phoebe: hardware is stateful and dangerous, data must not drop, access is exclusive and
serialized, cancellation must reach the physical device. Every one of AstrBot's core
dispatch choices is wrong for Phoebe — and Phoebe's spec already understood this
(control/data plane split). What transfers is the **operational shell**: registration
metadata, lifecycle hygiene, observability plumbing, config→UI pipeline, HTTP boundary
patterns.

---

## 1. Diagnosis: consolidated issue inventory (abridged)

Severity-ranked; full evidence in the panel reports. IDs used below reappear in the plan.

### Critical
| ID | Issue | Evidence |
|----|-------|----------|
| C1 | Writer-task death (open failure, metric-write failure) leaves producers awaiting unresolved futures; run unkillable (cancel is checkpoint-only), leases go stale, `aclose` can block on a full queue | `writer.py:151-156, 196, 201-204`; `task_manager.py:105-116, 360-363` |
| C2 | App shutdown never drains active runs: `AppRuntime.shutdown` disconnects controllers under a live run, then `LoopThread.stop()` kills the loop with the run's `finally` unexecuted — no `stop()/safe_state()`, writer unclosed, leases unreleased | `bootstrap.py:36-39, 106-108`; `ui/app.py:54-58` |

### High
| ID | Issue | Evidence |
|----|-------|----------|
| H1 | Pause→cancel race: cancel doesn't clear `pause_requested`; next checkpoint attempts `STOPPING→PAUSED`, an illegal transition → run misreported FAILED | `task_manager.py:105-116, 258-268` |
| H2 | Any failure before RUNNING (stage/manifest/baseline) → illegal `QUEUED→FAILED` → RuntimeError → zombie run, final rebroadcast carries QUEUED | `task_manager.py:44-55, 326-348` |
| H3 | Run setup (mkdir, sink add, writer start) sits before the `try` — an OSError leaks leases + sink with no terminal event | `task_manager.py:300-315` |
| H4 | Reaper reclaims devices but not the run; later `release()` pops the *new* holder's row → two runs can co-drive one device | `device_manager.py:167-172, 201-221` |
| H5 | Paused/suspended runs stop heartbeating → healthy runs get reaped after TTL (Suspender exists precisely for hours-long pauses) | `task_manager.py:107-114`; `device_manager.py:201-221` |
| H6 | No reconnection story at all; startup all-or-nothing; health checked exactly once (`ui/app.py:52`); transports keep dead handles | `device_manager.py:50-64`; `tcp.py:118-146`; `visa.py:110-120` |
| H7 | Real RTO6 can never pass identity verification: config vendor `"rohde-schwarz"` (registry key) can't substring-match `ROHDE&SCHWARZ` (IDN) | `registry.py:27`; `device_manager.py:70-78`; `rs_rto6/controller.py:87-96` |
| H8 | VISA binary reads honor `read_termination="\n"` → float32 blocks truncate at ~1/256 probability per byte | `visa.py:76-79, 130-137` |
| H9 | TCP binary read: empty `recv` → infinite busy-loop pinning the device worker at 100% CPU | `tcp.py:163-172` |
| H10 | Santec frame display leaks a multi-MB temp CSV per frame (~90 GB per 10k-step run) | `santec_slm200/driver.py:176-184` |
| H11 | Queued runs stall forever when leases are freed by the reaper (queue only re-checked in `_execute` finally) | `task_manager.py:373, 408-419` |
| H12 | UI command surface hardcoded; form defaults already drifted from plugin contract defaults; every new plugin = editing `main_window.py` | `main_window.py:101-132` vs `tpa_multiplier.py:31-35` |
| H13 | No CI, no import-linter, no lint/type gate; layering invariants enforced by convention only | `pyproject.toml:27-29`; no `.github/` |
| H14 | Run outcome never persisted (manifest written pre-RUNNING only); drop counters (`total_dropped`) never surfaced; failure path (`RunState.FAILED`, `safe_state`) has zero tests | `task_manager.py:326`; `bus.py:140-148`; test suite |

### Medium (selected, thematic)
- **Stringly control protocol**: `"423"`, `reason=="final"`, `event_type` strings, prose-only `CommandAck.reason` (`gateway.py:32-37`; `task_manager.py:221-222, 371`).
- **Encapsulation leaks**: `_dm._lease_sets` (`task_manager.py:270`), `_tm._records` (`task_manager.py:487,497`); DI resolver snapshots inventory at construction (`task_manager.py:189-193`).
- **Retained/topic model per-event-type, not per-entity** — late subscriber sees one device's health, one run's state (`bus.py:128`; `events.py:133-134`).
- **Fire-and-forget `asyncio.create_task`** for hardware stop, no strong ref (`task_manager.py:273`); `_records` never evicted (`task_manager.py:187`).
- **Role-based DI never cross-checks kind; injection never verifies protocol conformance** — TOML typo becomes mid-run `AttributeError` (`di.py:88-95`; `task_manager.py:321-331`).
- **Lease inheritance implemented but unreachable** (no production caller passes `parent`; `LeaseSet.merge` sharing the parent's whole slot dict would over-release if it were used) (`lease.py:97-114`; `task_manager.py:218`).
- **Health/snapshot bypass op-lock**, interleaving SCPI into acquisitions (`yokogawa_aq637x/controller.py:111-131`).
- **Vendor SCPI mnemonics in domain models** (`domain/scope.py:24-37`).
- **Preview pipeline spectrum-shaped end-to-end** (`events.py:53-65`; `main_window.py:212-215`) — no vehicle for scope/DAQ/camera previews.
- **Sim/real option asymmetry** breaks "same config runs under sim" (`sim/controllers.py:97` vs `Slm200Options`).
- **Bindings dead because plugins hardcode `Depends(role=...)`** (`tpa_multiplier.py:48-49`; `di.py:88`).
- **Plugin log events never reach the UI** (`LogEvent` published by nobody).
- **No durability**: no fsync/SWMR; mid-run HDF5 unreadable by any external reader (`writer.py:202, 237`).

### Strengths to preserve (explicitly out of refactoring scope)
Control/data plane separation and its mechanical enforcement; `ContractModel` strictness +
`validate_boundary` choke points; lease ownership table with atomic try-all-or-release-all;
checkpoint as the unified pause/cancel/heartbeat/yield point; ordered, bounded, degrading
cleanup with the final-rebroadcast protocol; per-run loguru sink discipline; worker
thread-affinity model with DLL initializer + Win32 pump; run-directory provenance
(manifest + baselines + dual timestamps); the sim physics closed loop; the L4
architecture test (`test_e2e_sim.py:71-117`).

---

## 2. What to take from AstrBot (independent triage)

### Adopt directly (high value, low adaptation)
1. **Retry/backoff classifier as one shared utility** (`provider/sources/request_retry.py`) —
   AstrBot's three divergent hand-rolled adapter retry stacks are the cautionary tale.
   Phoebe: one `phoebe/core/retry.py` with transient-vs-fatal classification
   (VISA timeout / ConnectionError / DeviceReportedError...), exponential backoff + ceiling,
   per-instrument labels.
2. **Controller operational state on the base class** (`platform/platform.py:20-119`):
   status enum (PENDING/READY/DEGRADED/ERROR/OFFLINE), capped error ring, started-at,
   `get_stats()`. Costs nothing; the UI and health system need it.
3. **Log broker + SSE replay contract** (`core/log.py:126-147`, `log_service.py:29-63`):
   ring cache + bounded per-subscriber queues + `Last-Event-ID` replay. Same architecture
   as Phoebe's bus; fills the missing "reconnecting client" story and the dead `LogEvent`
   path. Adaptation: publish loop-safely (AstrBot's cross-thread `put_nowait` is a bug).
4. **Event-loop diagnostics** (`event_loop_diagnostics.py`): lag monitor + faulthandler
   watchdog that dumps all thread stacks when the loop wedges. Phoebe's single LoopThread +
   blocking-DLL topology is exactly where this pays off.
5. **Strong-ref task set + done-callback exception logging** (`event_bus.py:37, 52-63`)
   for every fire-and-forget task (fixes the `_safe_stop` hazard).
6. **Single-instance file lock** (`cli/commands/cmd_run.py:63-66`) — two processes opening
   one VISA resource is a hardware fault.
7. **Failed-plugin records + per-plugin fault isolation at load** (`star_manager.py:771-833`).
8. **PEP 440 `phoebe_version` compat gate in plugin metadata** (`star_manager.py:625-657`).
9. **Cheap per-kind health probes** (`provider.py:207-317`): OSA = IDN + minimal sweep,
   DAQ = read one sample — invoked through the op-lock (fixes lock-bypassing health reads).
10. **Atomic config writes** (`astrbot_config.py:234-251`); **response envelope + typed
    ApiError + global handler** (`responses.py`, `api/app.py:149-163`); **task + progress-poll
    for long HTTP ops** (`update_service.py:101-191`) — all for the service layer phase.
11. **Fatal-vs-transient classification in reconnect loops** (`tg_adapter.py:270-274`):
    wrong address / missing DLL must surface, not retry forever.

### Adopt with adaptation
1. **Schema-as-UI-metadata pipeline** — the headline transfer. AstrBot proves one
   declarative schema can drive widget choice, grouping, conditional visibility, i18n and
   validation with zero frontend code per feature (verified end-to-end into
   `ConfigItemRenderer.vue`). But its hand-written triple-generation dict monolith
   (`default.py`, 4.4k lines, validation still bound to the deprecated generation) is the
   proven failure mode. **Phoebe must generate the metadata from its existing pydantic
   contracts** (`model_json_schema()` + `json_schema_extra` for hints/widgets/conditions),
   so schema, validation and UI cannot drift. Applies to: plugin config forms (replaces
   hand-written Qt forms, fixes H12), instrument options, and command payloads.
2. **Threshold-triggered client rebuild + backoff** (telegram/KOOK reconnect loops) — as a
   *policy object* injected into controllers, with the rebuild executing on the device's
   worker thread (DLL affinity), exponential backoff, give-up ceiling.
3. **Hot add/remove of device instances keyed by config id with orphan sweep**
   (`platform/manager.py:256-265`) — gated on leases (423 an in-use instrument's teardown).
4. **Plugin discovery by directory + manifest** (`star_manager.py:192-311` shape, not code):
   `plugins/<name>/{plugin.toml, main.py}`; explicit import remains for builtins.
   Enable/disable without instantiation. **No module hot-reload while leases held.**
5. **`phoebe.api` façade package** (AstrBot's `astrbot/api/` discipline): plugins import
   only `phoebe.api`; core internals can then move freely.
6. **Structured identity with canonical string form** (`MessageSession`) for run/lease/
   instrument ids — as frozen ContractModels, with an encoding that never needs sanitizing.
7. **Onion pipeline for the command path** — *maybe*, later, only if command-path
   cross-cutting policy (auth, interlocks, audit) accumulates; with typed stage context,
   never an `_extras` dict. Not needed at today's scale (one Gateway, three builtins).

### Do not copy (domain break or proven debt)
- Unbounded queue + task-per-event dispatch; broadcast filter scans (Phoebe's addressed
  commands + leases are strictly better for hardware).
- Mutable god-event with untyped `_extras`; stop-flag duality.
- Config as live mutable dict subclass; `__getattr__→None`; **silent pruning of unknown
  keys** (data loss); in-place mutation by services.
- Import-time global composition (`astrbot/core/__init__.py`) — breaks sim/testability.
- `os.execv` restart + psutil child-killing (instruments need ordered release).
- Error-string-matching capability discovery (SCPI has error queues; classify by code).
- In-process pip (`pip._internal` on a live hardware interpreter).
- Provider failover/API-key rotation semantics (never auto-substitute instruments).
- Dual legacy/v1 route surface & Flask-compat shim (lesson: version the API from day one).
- match/case lazy-import switchboard (Phoebe's factory registry already better).
- `_task_wrapper` double-task supervision (silently breaks cancellation — wrap coroutines,
  not tasks).

---

## 3. Target architecture (first draft)

### 3.1 Layering (dependency direction strictly downward)

```
┌────────────────────────────────────────────────────────────┐
│ Frontends: PyQt shell (today) · Vue/Tauri (later)          │
│   — consume ONLY the service layer (in-proc or HTTP)       │
├────────────────────────────────────────────────────────────┤
│ Service layer (new): RunService · DeviceService ·          │
│   PluginService · ConfigService · EventStreamService       │
│   — query + command + subscribe; envelope + error codes;   │
│     owns serialization; later wrapped by FastAPI routes    │
├────────────────────────────────────────────────────────────┤
│ Core kernel (existing, hardened): Gateway · TaskManager    │
│   (run FSM) · DeviceManager (device FSM + leases) · DI ·   │
│   EventBus · RunWriter · CapabilityRegistry                │
├────────────────────────────────────────────────────────────┤
│ Instrument stack: Controller (+op-state, +reconnect        │
│   skeleton) → Driver → Transport → per-device Worker       │
├────────────────────────────────────────────────────────────┤
│ Contracts (`phoebe/contracts` or expanded core): models,   │
│   ids, error codes, events, commands, schema export        │
└────────────────────────────────────────────────────────────┘
   plugins/* depend only on `phoebe.api` (façade over
   contracts + ctx + Depends + register)
```

### 3.2 The two state machines to complete

**Run FSM** — add `PREPARING` (between QUEUED and RUNNING: run-dir/sink/writer/stage) and
`FINALIZING` (cleanup in progress); make every setup step inside try/finally; terminal
record eviction; persist a `RunResult` json at terminal.

**Device FSM** (new, owned by DeviceManager per instrument):
`CONFIGURED → CONNECTING → READY ⇄ DEGRADED → ERROR/OFFLINE`, with reconnect policy
(classifier + backoff + rebuild-on-worker-thread), health poller feeding
`DeviceHealthEvent`, lease acquisition legal only in READY, startup tolerant of
offline devices (boot degraded, not all-or-nothing).

### 3.3 Protocol hardening (contracts v2)
- `AckCode`/`ErrorCode` enums (`OK, REJECTED_BUSY, REJECTED_INVALID_PAYLOAD,
  REJECTED_UNKNOWN_COMMAND, REJECTED_UNBOUND_DEPENDENCY, ...`) + structured `ErrorInfo`
  (code, message, instrument_id, detail) in acks and `ErrorEvent`.
- `RunStateEvent.final: bool` field replaces the `reason=="final"` magic string.
- Per-entity topics (`run_state/<task_id>`, `device_health/<instrument_id>`) or retained
  maps keyed by entity; bounded replay ring (seq-addressable) for reconnecting clients.
- JSON Schema export command (`python -m phoebe.contracts.export`) + a round-trip test
  that serializes every event type through the `GatewayEvent` union (makes the union alive).
- Generic preview vehicle: `PreviewPayload` union (spectrum / waveform / image-thumbnail /
  scalar-series) replacing the spectrum-only `TracePreview`.

### 3.4 Plugin platform
- `plugins/<name>/plugin.toml` manifest: id, version, `phoebe_version` specifier,
  entry module, config model reference, UI hints. Directory discovery + explicit builtins.
- Per-plugin failure records; enable/disable; instance-scoped `PluginRegistry` (kill the
  module-global singleton); `phoebe.api` façade as the only sanctioned import surface.
- Plugin config schema (pydantic) exported → auto-generated forms (Qt now via a small
  schema-driven form builder, Vue later from the same JSON Schema).

---

## 4. Phased plan (first draft)

**Phase A — Stabilize the kernel (highest priority, no new features).**
Fix C1, C2, H1–H5, H11; add `PREPARING/FINALIZING`; writer error → fail-fast into the run
(resolve producer futures with the error, cancel token trip); shutdown drains runs
(request_cancel all → bounded wait → safe_state → then disconnect); reaper terminates the
run it reaps + lease release checks lease identity; heartbeat during PAUSED (periodic
touch while parked, or TTL suspension when paused-by-design). Add the missing failure-path
tests (plugin raises; writer dies; reaper enabled; pause→cancel; queue policy; suspender).
Stand up CI: pytest + ruff + pyright (contracts/core first) + import-linter with the
§18-13 layer contract.

**Phase B — Device lifecycle & recovery.**
Device FSM + reconnect policy + health poller + degraded startup; controller op-state +
error ring; retry classifier utility; fix H7 (identity normalization), H8/H9 (binary read
correctness — testable with pyvisa-sim/mock), H10 (temp-file lifecycle); single-instance
lock; loop watchdog/lag monitor; sim fault injection (disconnects/timeouts) so all of
this is testable offline.

**Phase C — Service layer & protocol v2.**
Error codes + ack/event hardening; RunResult persistence + run catalog query; the five
services; schema export + codegen check; per-entity retained/replay; PyQt refactored to
consume services in-process (proves the boundary; deletes reach-ins like
`ui/app.py:52`). The Start-gate rule moves server-side (a `dispatchable` flag).

**Phase D — Plugin platform.**
Manifest + discovery + failure records + enable/disable + `phoebe.api`; schema-driven
form generation (replaces hand-written Qt forms, fixes H12 and the drift class);
plugin-authored capability docs auto-listed in UI.

**Phase E — Web frontend (Tauri/Vue).**
FastAPI app wrapping the same services (envelope + ApiError + JWT-lite + SSE/WS with
replay); static dist with version pinning; Vue forms generated from the exported schemas;
PyQt shell retired or kept as a maintenance fallback.

Ordering rationale: A before everything (correctness debt compounds; every later phase
builds on run/device lifecycle). B before C (the service layer should expose the *real*
device model, not the current one-shot health snapshot). C before D/E (both consume the
service surface). D and E can interleave.

---

## 5. Costs and alternatives considered

- **Do nothing / features first**: rejected — C1/C2/H4/H5 are data-loss or
  hardware-unsafe classes triggered by routine events (disk full, window close, long
  pause). They will fire during the first serious overnight campaign.
- **Adopt Bluesky instead of hardening TaskManager**: the spec's own stop-loss criterion
  (§17) — rewind/re-plan/resource-graph scheduling — has not been hit; current gaps are
  bugs, not missing RunEngine-class capabilities. Keep the criterion, don't switch now.
- **Jump straight to the web frontend**: rejected — without the service layer the Vue
  app would bind to today's in-process idioms (magic strings, per-type retained events)
  and harden them; the PyQt-as-first-client step is cheap and de-risks the protocol.
- **Full DI container / framework**: rejected — composition root + explicit wiring at
  Phoebe's scale (~10 components) is clearer; the existing `Depends` mechanism for
  plugins is the only DI that earns its keep.
- **Microservice/per-device daemon split now**: rejected — spec Phase 4 trigger
  (a DLL device destabilizing the main process) hasn't occurred; the worker-thread
  isolation is holding.
