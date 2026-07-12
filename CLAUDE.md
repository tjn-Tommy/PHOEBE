# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

PHOEBE is a unified lab-instrument control platform (SLM / OSA / Scope / DAQ / AWG) implemented per the architecture spec in **refactor.md** — that document is authoritative; README.md has a path → spec-section mapping table. The top-level dirs `awg5204/` and `TPA_experiment/` are the two legacy codebases it replaced: they are deliberately git-ignored, kept as read-only reference, and **must never be imported from `phoebe/`**.

## Commands
use conda env: phoebe

```powershell
pip install -e .[dev,ui]                            # core + pytest + PyQt5/pyqtgraph
python -m pytest tests/ -q                          # full suite (runs fully offline via sim backend)
python -m pytest tests/test_e2e_sim.py -q           # one file
python -m pytest tests/ -k "pause" -q               # by keyword
python examples/run_sim_demo.py                     # headless end-to-end demo
python -m phoebe.ui.app --config config/sim.toml    # PyQt5 GUI (sim mode, no hardware)
python -m phoebe.server --config config/sim.toml    # HTTP API + web UI (needs .[server])
ruff check phoebe tests examples tools              # lint gate (CI-enforced)
lint-imports                                        # architecture layer contracts (CI-enforced)
pyright                                             # type gate, contracts/core/domain/services scope (CI-enforced)
python -m phoebe.contracts.export                   # regenerate schemas/ after ANY contract change (CI checks drift)
python tools/gen_ts_types.py                        # regenerate the TS sample consumer types (CI checks drift)
```

- pytest runs with `asyncio_mode = "auto"` — async test functions need no decorator.
- Everything works with only the core deps (pydantic/numpy/h5py/loguru): `pyvisa`, `nidaqmx`, `PyQt5`, and `fastapi`/`uvicorn` are imported lazily so sim mode and tests never require them. Keep it that way — never import them at module top level outside `phoebe/transports/visa.py`, `phoebe/instruments/ni_daq/`, `phoebe/ui/`, and `phoebe/server/` (`tests/test_server_api.py` importorskips fastapi).
- Real hardware: set `backend = "real"` per instrument in the TOML config; extras `.[visa]` (OSA/Scope/AWG) and `.[daq]` (NI-DAQ).
- The UI is **PyQt5** by explicit user choice — do not migrate to PySide6/PyQt6.

## Architecture (the parts that span multiple files)

**Control plane vs data plane.** Commands enter through `Gateway` (`core/gateway.py`) as `CommandEnvelope` → `TaskManager.dispatch` — must-deliver. Observations fan out through `EventBus` (`core/bus.py`) with bounded per-subscriber queues — droppable by design. Bulk arrays **never** ride the bus: `RunWriter` (`core/writer.py`) is the sole HDF5 writer; the bus only carries `DataPointerEvent`s with previews capped at 256 points (a dev-mode assert rejects >64KB events).

**Contracts.** Everything serializable lives in `phoebe/contracts/` (bottom layer, no phoebe-internal deps; the old `core/contracts.py`, `core/events.py`, `core/errors.py` are re-export shims — import from `phoebe.contracts` in new code). All boundary models subclass `ContractModel`: frozen, strict, extra=forbid. Strict python-mode validation rejects list→tuple and nested dicts, so any dict payload crossing a boundary must go through `validate_boundary()` (JSON-round-trip validation). It is already applied at the choke points — `parse_app_config`, the admission chain in `TaskManager.dispatch`, `CapabilityRegistry.invoke` — use it, don't call `model_validate` on raw dicts. After ANY contract change run `python -m phoebe.contracts.export` + `python tools/gen_ts_types.py` and commit the regenerated `schemas/` + `examples/ts_consumer/` files, or CI fails on drift.

**Instrument stack.** Composition, not inheritance: `Transport` (`transports/`) → `Driver` (pure protocol translation, no locks/asyncio) → `Controller` (`core/controller.py` base: op-lock, settled semantics, `stop()`/`safe_state()` that bypass the op-lock, `stage()`/`unstage()`). Controllers expose capability **Protocols** defined in `instruments/protocols.py` (5 kinds: SLM/OSA/Scope/DAQ/AWG). New instrument = `phoebe/instruments/<vendor_model>/{driver,controller}.py` + a factory registration in `instruments/registry.py` keyed by (kind, vendor, model); `backend = "sim"` routes to the sim factories instead.

**Runs.** `TaskManager` (`core/task_manager.py`) owns the run state machine (QUEUED→RUNNING→…→ terminal). Dispatch runs a fixed typed **admission chain** (`core/admission.py`: route/validate → `CommandLedger` idempotency (`core/command_ledger.py` — reusing a command_id replays the first ack; new ids per attempt!) → maintenance → plugin API → DI → device health) and every ack carries a stable `AckCode` — branch on codes, never parse `reason`. Plugins call `await ctx.checkpoint(...)` which is simultaneously the pause point, cancel point, and lease heartbeat. Leases (`core/lease.py`, `core/device_manager.py`) are acquired try-all-or-release-all; a busy instrument yields `AckCode.DEVICE_BUSY` (or queues, per `dispatch_policy`). Every run appends lifecycle facts to `journal.jsonl` (`core/journal.py`; two axes: `execution_outcome` vs `finalized ok|degraded`), indexed in the SQLite catalog under `runs/.phoebe/` (`core/catalog.py`, rebuildable); bootstrap's recovery scan explains crashed runs from files alone. The terminal `RunStateEvent` is **re-broadcast with `final=True` after leases are released and cleanup ran** — anything starting the next run must gate on that flag, not the first terminal event, or the next dispatch races the lease release and gets DEVICE_BUSY.

**Plugins** (`phoebe/plugins/`, pattern: `tpa_multiplier.py`): `@register(plugin_id=...)` class + `@on_command("...")` async method taking `(config, ctx, instrument=Depends(role="..."))`. Roles resolve via `[plugins."<id>".bindings]` in the TOML config (`core/di.py`). Hard rules (refactor.md §18): plugin code contains **zero locks, zero Driver imports, zero manual sleeps** — settling lives in controller options (e.g. `SlmOptions.settle_ms`), waiting lives in `ctx.checkpoint`/controller internals.

**Threading.** Three tiers: Qt main thread (UI only) / one dedicated asyncio `LoopThread` (all of phoebe core, `app/bootstrap.py`) / per-device `BlockingDeviceWorker` threads (`core/worker.py`) for blocking SDK calls — the Santec SLM worker uses `pump=True` + a load-DLL initializer so the vendor DLL and its Win32 message pump never leave that thread. The UI talks only to `phoebe/services/` (`ServiceHub.call(coro)` = `run_coroutine_threadsafe` in; `UiEventBridge`'s `pyqtSignal` out, `ui/bridge.py`) — import-linter forbids UI→core-internal imports. Never show modal dialogs on command-ack paths (`ui/main_window.py` uses statusbar + log instead — modals break offscreen/automated runs).

**HTTP adapter** (`phoebe/server/`, Phase E): FastAPI over the same `phoebe/services/` surface the UI uses — an import-linter contract forbids server→core-internal imports too. Everything under `/api/v1` returns the `ApiEnvelope` (`status: ok|warning|error`); domain rejections are `CommandAck`s inside `data` (branch on `data.code`), `ApiError` is transport-level only. SSE lives at `/api/v1/events/stream` with `Last-Event-ID`/`since_seq` gap repair from the bus replay ring. The zero-build web client in `phoebe/server/static/` is gated by the A14 version-pin cascade — its `version` file must equal `CONTRACTS_VERSION` or the server refuses/flags it (bump it when contracts change). Security ladder is fail-closed in `server/auth.py`: loopback binds get a per-process token (printed, never logged); non-loopback requires explicit token + `role = "read_only"`. Mutating routes audit to `runs/.phoebe/audit.jsonl`. Threat model: `docs/architecture-review/SERVER_THREAT_MODEL.md`.

**Simulation.** `instruments/sim/` implements all five controllers over a shared `SimContext` whose physics couples the SLM mask to the OSA spectrum (mask coherence → peak height), so optimizer loops close offline with meaningful feedback. Test doubles for lower layers live in `transports/mock.py` (`MockScpiTransport` with fnmatch rules, `TranscriptReplayTransport` for replaying recorded real-device sessions — no transcripts recorded yet).

## Known not-yet-migrated (don't "clean up" the legacy dirs)

The legacy GUIs' specialty pages (calibration/encoding analysis) and the analysis/optimization scripts (`TPA_experiment/src/slm_module/{optimization,tpa_phase,analysis,calibration}*.py`) are intentionally unported; they should be migrated one at a time as plugins/panels when asked.
