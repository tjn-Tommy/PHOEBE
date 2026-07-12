# Spec-vs-Code Drift & Test Coverage Report — PHOEBE

Scope: refactor.md (read in full, 1466 lines), README.md, CLAUDE.md, the six test files (read in full), with spot-checks of every core implementation file cited below. The suite was executed: **35 passed in 1.53s** (matches README.md:18's "35 项测试" claim) — but only after installing pytest into the designated conda env, see Issue 2.

## 1. Responsibility map (as observed)

**Spec (refactor.md)** is a v2 architecture document organized in §1–§18 plus a phase plan (§17, refactor.md:1412) and 13 hard invariants (§18, refactor.md:1420-1436). README.md:25-49 maps every path to a spec section; I verified this table is accurate (e.g. `core/bus.py` really implements §9's fan-out/retained/throttle — bus.py:75-202; `core/writer.py` really implements §10's single-writer/backpressure/parquet — writer.py:117-279).

**Test files:**

- `tests/conftest.py:7` — a single `sys.path.insert` so tests run against the repo checkout without installation. No shared fixtures; each e2e-style file builds its own runtime.
- `tests/test_contracts.py` (9 tests) — §3: strict no-string-coercion (:14-17), int→float allowed (:19-21), physical bounds (:24-26), model_validator cross-field (:29-32), extra=forbid (:35-38), frozen events (:41-44), preview cap 256 (:47-49), TOML-dict parse + role bindings (:52-66), duplicate-role rejection (:69-77).
- `tests/test_bus_and_worker.py` (10 tests) — §9/§12.3: fan-out + seq stamping (:20-29), retained snapshot for late subscriber (:32-38), DROP_OLDEST with drop counting (:41-49), ERROR policy raising `BusOverflowError` (:52-58), cross-thread `publish_threadsafe` (:61-71), trailing-edge throttle (:74-87), one-thread-per-worker (:90-102), exception relay (:105-113), initializer (DLL-load) failure surfacing (:116-124), pool one-worker-per-device identity (:127-134).
- `tests/test_drivers_l1.py` (5 tests) — §14.1 L1, **AQ637X only**: exact SCPI command stream + meters→nm parse (:42-59), software averaging repeats `:INITiate` (:62-70), find-peaks capability (:73-81), dict payload validated at the registry choke point incl. a bad-type rejection (:84-96), `stop()` sends `:ABORt` (:99-103).
- `tests/test_lease_and_di.py` (4 tests) — §6/§7 against a real sim runtime (fixture at :22-41, **`start_reaper=False`**): all-or-nothing acquisition leaves the second device free (:44-57), inheritance refcount semantics (:59-72), DI resolution by role/binding/uniqueness (:75-94), unknown role fails fast (:97-108).
- `tests/test_e2e_sim.py` (7 tests) — §14.3 L4, the strongest file: a full TPA run asserting the §10.5 reproducibility checklist item by item — manifest fields incl. `settle_ms`/`lut_id`, baselines, jsonl, HDF5 shapes, attrs round-trip, metrics rows, pointer events with ≤256-point previews, `total_dropped()==0`, `active_lease_count()==0` (:71-117); grid scan via the sweep helper (:119-128); 423 rejection while running (:131-139); pause→resume→cancel lifecycle with the cancel going through the gateway built-in (:142-165); invalid payload rejected at dispatch (:168-172); unknown command (:175-177); and a physics assertion that a coherent mask beats a random one by >10 dB — the optimizer's signal (:180-201).

**Call chains verified in code** (not just documented): `Gateway.submit` → builtin or `TaskManager.dispatch` (gateway.py:47-50) → `validate_boundary` payload choke (task_manager.py:204) → `DependencyResolver.resolve` (di.py:68-112) → `DeviceManager.try_acquire_all` sync/atomic with rollback that also decrefs inherited entries (device_manager.py:123-165) → `_execute`: manifest+baseline → `stage()` → RUNNING → plugin → finally: `stop()`/`safe_state()`-on-failure/`unstage()` under `asyncio.timeout` → writer close → post baseline → release → sink removal → terminal `RunStateEvent` re-broadcast with `reason="final"` → next queued run (task_manager.py:296-373). The UI genuinely gates its Start button on that final rebroadcast (main_window.py:336-337), matching CLAUDE.md's claim.

## 2. Strengths

- **The L4 test is an architecture test, not a feature test.** test_e2e_sim.py:71-117 simultaneously asserts data-plane completeness, control-plane pointer discipline, zero bus drops, and zero lease leaks after a real dispatch→cleanup cycle. This is precisely §14.3's CI-gate content, implemented faithfully.
- **The sim backend closes the loop with real physics.** `TpaPhysicsModel.coherence` (sim/context.py:34-38) makes the OSA spectrum a monotone function of SLM mask quality, and test_e2e_sim.py:180-201 proves the optimizer signal exists offline. This is the single biggest enabler for hardware-free development.
- **Contract choke points exist and are exercised from both directions**: dict payloads through `validate_boundary` at dispatch (task_manager.py:204, tested at test_e2e_sim.py:168-172) and at capability invoke (capability.py:125-128, tested with both a passing and failing dict at test_drivers_l1.py:84-96).
- **Lease atomicity tests assert the negative space**: test_lease_and_di.py:51-53 checks the *second* device stays free after a failed acquisition — the actual no-hold-and-wait property, not just the exception.
- **Worker tests cover the DLL-relevant failure mode**: initializer failure surfaces on first `call()` rather than being swallowed (worker.py:122-137, test_bus_and_worker.py:116-124).
- **The suite is fast (1.53s) and hardware-free**, honoring §18 rule 12; all sim delays are `asyncio.sleep`-based so most assertions are deterministic.

## 3. Issues

**Issue 1 — No CI, no import-linter, no lint/type gate at all. Severity: High.** Spec §14.3 makes L4 a "CI gate" and §18 rule 13 mandates import-linter layering (refactor.md:1355, 1436); §4.3 and §13.2 both say the Driver-import ban is "由 import-linter 在 CI 强制" (refactor.md:414, 1307). Observed: no `.github/` at the repo root (verified by directory listing; the workflows Glob found belong to the unrelated `AstrBot/` subproject), no import-linter/ruff/pyright config anywhere, pyproject.toml:27-29 contains only pytest settings. Impact: every layering invariant is enforced by convention; a plugin importing a Driver would merge silently. Trigger: any contributor PR.

**Issue 2 — The documented test environment cannot run the suite; a conditional assert silently skips the Parquet mechanism. Severity: High (silent misbehavior).** CLAUDE.md instructs `conda env: phoebe` + `python -m pytest`; that env had no pytest installed (observed: `No module named pytest`; I installed it to run the suite) and **no pyarrow** (observed: import failure). Consequently test_e2e_sim.py:104-106 (`if (run_dir / "metrics.parquet").exists():`) silently skips, so the §10.2 compaction path writer.py:252-279 has never executed in a passing suite on this machine — an ImportError-guarded feature whose test guard mirrors the same ImportError (writer.py:258-261). Impact: a regression in compaction is invisible. Trigger: run suite in an env without pyarrow — it's green either way.

**Issue 3 — A paused run stops heartbeating; the TTL reaper will reclaim devices out from under it. Severity: High.** `checkpoint` touches leases only on entry, then blocks on `resume_event.wait()` (task_manager.py:107-112) — no touches while PAUSED. The reaper reclaims any lease untouched for `ttl_s` (default 600s, config.py:91), calling `stop()`+`safe_state()` and popping ownership (device_manager.py:201-221). But §8.4's Suspender exists precisely for **hours-long** suspensions (refactor.md:903-914). After recovery, `request_resume` happily resumes a run whose leases are gone; a concurrent dispatch can then acquire the same instruments and interleave operations — the exact hazard leases exist to prevent. Untestable today because both runtime fixtures pass `start_reaper=False` (test_e2e_sim.py:52, test_lease_and_di.py:39). Trigger: suspender pause > 10 min with the reaper on (the production default — bootstrap.py:62-63). UNCERTAIN whether resume-after-reap actually proceeds without error (nothing re-checks ownership on resume; `release` uses `pop(..., None)` so it won't even raise — device_manager.py:167-172); confirming needs a reaper-enabled test.

**Issue 4 — The failure path is completely untested: no test ever produces RunState.FAILED.** Severity: High. `safe_state()` is invoked only when `failed=True` (task_manager.py:354-355); no test raises from a plugin, so safe-state-on-failure, `ErrorEvent` publication (task_manager.py:342-345), cleanup-timeout degradation (:351-358), and writer-error relay into the producer (writer.py:193-194) have zero coverage. The spec calls cleanup "无条件、有序、有超时" (§8.5) — the most safety-critical code in the platform is exercised only on the happy and cancel paths. Trigger: first real hardware exception.

**Issue 5 — §3.6 Schema export is unimplemented — the named down-payment on the web-frontend promise.** Severity: Medium (High once Phase 3 starts). No `model_json_schema()` call exists anywhere in `phoebe/` (grep over the tree); `GatewayEvent` is defined (events.py:120-130) but nothing consumes it — no serializer, no export script. §13.3's "核心零改动" claim rests on this (refactor.md:343, 1311). The models make it a small task, but today the promise is untested and unproven.

**Issue 6 — §3.6 prod response-validation switch not wired.** Severity: Medium. `CapabilityRegistry(validate_responses=True)` is hard-defaulted (capability.py:96-99) and `InstrumentController.__init__` never receives the app `mode` (controller.py:59-64); grep shows no code path connecting `AppConfig.mode` to it. Spec: 响应校验 prod 可降为抽样 (refactor.md:342). Impact: none today, hot-path validation cost later; also means the documented dev/prod behavioral difference doesn't exist, so it can't be tested.

**Issue 7 — Periodic health checks don't exist.** Severity: Medium. §5.4 requires "周期 health 检查（结果以 DeviceHealthEvent 上总线）" (refactor.md:708). `health_check_all` exists (device_manager.py:224-230) but is called exactly once, at UI startup (ui/app.py:52). Impact: a device going offline mid-session is never reported; a Suspender configured on `watch_topic="device_health"` (config.py:70) has no periodic event source, making half the Suspender feature dead in practice.

**Issue 8 — Phase-2 mechanisms implemented but with zero tests: Suspender, QUEUE dispatch policy, lease reaper.** Severity: Medium each. Suspender: task_manager.py:434-500 (subscription, grace window, auto-resume — all untested; note `_suspend_all`/`_resume_suspended` reach into `self._tm._records` privates at :487, 496). QUEUE policy: task_manager.py:223-229 + `_maybe_start_next_queued` :408-419 — never tested; e.g. whether a queued run whose device is *still* busy is correctly skipped-and-retained, or whether cancel-while-queued (:260-263) interacts correctly with the queue, is unverified. Reaper: device_manager.py:185-221, disabled in every fixture. §14.3 says CI must exercise pause/resume/cancel (done) — but these siblings escaped.

**Issue 9 — L1 coverage exists for 1 of 5 real drivers; L2 has zero transcripts.** Severity: Medium. test_drivers_l1.py covers only AQ637X. `santec_slm200/driver.py`, `rs_rto6/`, `ni_daq/`, `tek_awg5204/` have no tests at all, despite `MockScpiTransport` already supporting the binary-block rules the scope/AWG would need (mock.py:58-69). `TranscriptReplayTransport` is implemented (mock.py:75-120) but unused by any test, and README.md:67 admits no transcripts are recorded. §14.3 "CI 必跑：L1/L2 全量" (refactor.md:1355) is unmet on both halves. Trigger: first real-hardware session with the scope/AWG discovers protocol bugs the architecture was designed to catch offline.

**Issue 10 — The two "hard rule" runtime guards are `assert` statements.** Severity: Medium. The 64KB payload ceiling ("第二道防线", refactor.md:291) and the cross-thread-publish check are both plain asserts (bus.py:116-126). Under `python -O` both vanish silently; hard rule #1 (§18) then has only the schema-level preview cap left. Also, no test exercises the 64KB assert itself (only the 256-point preview cap, test_contracts.py:47-49).

**Issue 11 — Writer "backpressure" is actually synchronous write-through.** Severity: Low (documentation drift, perf ceiling). `append_array` awaits the per-item future to get its index (writer.py:151-156), so the producer blocks until the array is on disk; the bounded queue (`writer_queue_size`, config.py:79) never fills from a single plugin loop. Correctness is fine — it's *stronger* than §10.1's model — but the spec's pipelining ("写盘慢时背压自然传导" as a *queue* phenomenon, refactor.md:1047) doesn't happen, and acquisition can't overlap I/O. SWMR is also absent: `h5py.File(..., "w", libver="latest")` without `swmr_mode` (writer.py:202) vs. §10.1's explicit SWMR read path.

**Issue 12 — Sub-flow lease inheritance is only half-wired.** Severity: Low today. `DeviceManager.try_acquire_all` supports `parent=` and is tested (device_manager.py:127, test_lease_and_di.py:59-72), but `TaskManager.dispatch` never passes a parent (task_manager.py:218) and no API exists to dispatch a child task with `parent=ctx.leases` as §11.3/refactor.md:1186 describes. Same-coroutine sharing works by construction; task-level composition is a stub.

**Issue 13 — Small manifest/timestamp drifts.** Severity: Low. `RunManifest.code_version` is declared but always `""` (writer.py:76; never set at task_manager.py:393-401). Checkpoints log through loguru without the §10.4-mandated `t_mono_ns` (task_manager.py:106). Per-step `MaskRecipe`s are only persisted on spot-check steps (tpa_multiplier.py:60-71) — reconstruction of other steps requires replaying the shared RNG from the config seed, weaker than §10.3's "只存配方" letter. `LogEvent` is defined (events.py:110-116) but nothing ever publishes one — dead contract. `TaskManager._records` grows forever (task_manager.py:187, never pruned) — a slow leak in long interactive sessions. `request_cancel` reaches into `self._dm._lease_sets` privates (task_manager.py:270).

**Issue 14 — Mild timing dependence in tests.** Severity: Low. The e2e file uses fixed wall sleeps: 0.05s before asserting the first run still holds leases (test_e2e_sim.py:134 — safe because the 20-step sim run deterministically sleeps ≥100ms via settle+sweep, sim/context.py:56-57), 0.05s before asserting RUNNING after resume (:155-156 — a hard sleep, could flake on a badly loaded machine), and the throttle test compares wall-clock 0.12s against a 0.05s interval (test_bus_and_worker.py:78-87). The PAUSED assertion is properly a bounded 2s poll (:148-152). Overall flakiness risk is low but nonzero; only the resume assert and throttle test would benefit from event-based waits.

**Zero-test subsystems** (enumerated): the entire UI (`ui/app.py`, `ui/bridge.py`, `ui/main_window.py`), `bootstrap.LoopThread` (bootstrap.py:79-108), `gateway.submit_threadsafe` (gateway.py:52-55), real transports `tcp.py`/`visa.py` (including the hand-rolled IEEE-block reader tcp.py:158-188), identity-mismatch rejection (device_manager.py:66-78), four of five real drivers, sim scope/DAQ/AWG controllers (only SLM+OSA are driven by e2e), the Win32 pump branch (worker.py:139-160), and everything in Issues 3, 4, 8.

## 4. Extension pain points

- **New instrument kind**: the checklist is long and only conventionally enforced — protocol + `register_capability_kind` (protocols.py:98-103), domain models, driver, controller, factory key(s) (registry.py:19-32), a sim controller (factory.py:66-73 *requires* one per kind or `backend="sim"` fails), and L1 tests that today nobody would notice you skipped (Issue 9, Issue 1).
- **New plugin**: genuinely cheap (tpa_multiplier.py is the whole pattern), but the global `plugin_registry` with duplicate-command `ValueError` (plugin.py:69-73) makes repeated registration in test processes order-sensitive; test_e2e_sim.py works around it via module-level `load_builtin_plugins()` (:19) relying on import caching.
- **Reconnection/fault recovery**: there is no reconnection story at all — `connect_instrument` exists (device_manager.py:59-64) but nothing re-invokes it after a drop; no health poller exists to even notice the drop (Issue 7); and the pause-TTL interaction (Issue 3) means the recovery primitive that *does* exist (the reaper) can actively harm a live run. This is the least-specified area of both spec and code.
- **Web (Vue/Tauri) frontend**: the contracts are ready but the bindings aren't — no schema export (Issue 5), no serializer consuming `GatewayEvent`, and no test that round-trips an event through JSON, so drift between the Python models and any generated TS types would be undetectable today.

## 5. Coverage map: mechanism | spec § | implemented? | tested? | evidence

| Mechanism | § | Impl? | Tested? | Evidence |
|---|---|---|---|---|
| ContractModel strict/frozen/forbid | 3.2 | ✅ | ✅ | contracts.py:37-46; test_contracts.py:14-49 |
| validate_boundary choke (config/dispatch/capability) | 3.6 | ✅ | ✅ | contracts.py:81-91; task_manager.py:204; capability.py:125-128; test_drivers_l1.py:84-96 |
| Event closed union + preview cap | 3.4 | ✅ | ✅ (cap) | events.py:53-66,120-130; test_contracts.py:47-49 |
| 64KB dev assert | 3.4 | ✅ (assert) | ❌ | bus.py:121-126 |
| JSON-Schema export for codegen | 3.6/13.3 | ❌ | — | no `model_json_schema` in phoebe/ (grep) |
| prod response-validation switch | 3.6 | ❌ | — | capability.py:96-99,131 (always on) |
| TOML fail-fast config | 3.5 | ✅ | ◑ (dict path; file path untested) | config.py:109-132; test_contracts.py:52-77 |
| ScpiTransport family TCP/VISA | 4.2 | ✅ | ❌ (mock only) | tcp.py:40-188; visa.py:15-55 |
| SerialScpiTransport | 4.2 | ❌ | — | transports/ contains tcp/visa/mock only |
| Driver/Controller split, op-lock, settled | 4.3-4.4 | ✅ | ◑ (AQ637X only) | yokogawa…controller.py:145-184; sim controllers.py:104-110 |
| stop()/safe_state() | 4.4 | ✅ | ◑ (stop yes; safe_state never) | controller.py:96-105; test_drivers_l1.py:99-103 |
| stage()/unstage() | 4.4 | ✅ | ◑ (implicit) | task_manager.py:328-329,356 |
| CapabilityRegistry invoke choke | 4.5 | ✅ | ✅ | capability.py:115-133; test_drivers_l1.py:73-96 |
| 3-registry separation + sim routing | 5.1-5.2 | ✅ | ◑ (implicit via e2e) | factory.py:49-81 |
| Identity verification | 5.4 | ✅ | ❌ | device_manager.py:66-78 |
| Periodic health checks | 5.4 | ◑ (one-shot) | ❌ | device_manager.py:224-230; ui/app.py:52 |
| Lease atomic try-all-or-release-all | 6.2 | ✅ | ✅ | device_manager.py:123-165; test_lease_and_di.py:44-57 |
| Lease inheritance (refcount) | 6.3 | ✅ (DM); ◑ (no task-level parent wiring) | ✅ (DM level) | lease.py:97-114; task_manager.py:218 |
| TTL heartbeat + reaper | 6.4 | ✅ | ❌ (fixtures disable) | device_manager.py:185-221; test_e2e_sim.py:52 |
| DI Depends/bindings/uniqueness/fail-fast | 7 | ✅ | ✅ | di.py:68-112; test_lease_and_di.py:75-108 |
| Run state machine w/ legal transitions | 8.1 | ✅ | ✅ (legal paths) | task_manager.py:44-55,150-158 |
| Dispatch REJECT (423) | 8.2 | ✅ | ✅ | task_manager.py:220-222; test_e2e_sim.py:131-139 |
| Dispatch QUEUE | 8.2 | ✅ | ❌ | task_manager.py:223-229,408-419 |
| checkpoint pause/cancel/heartbeat | 8.3 | ✅ | ✅ | task_manager.py:105-116; test_e2e_sim.py:142-165 |
| Suspender | 8.4 | ✅ | ❌ | task_manager.py:434-500 |
| Ordered, timed cleanup + final rebroadcast | 8.5 | ✅ | ◑ (happy/cancel; FAILED never) | task_manager.py:349-373 |
| Per-run loguru sink add/remove | 8.6 | ✅ | ◑ (file exists) | task_manager.py:303-306,369; test_e2e_sim.py:91 |
| Bus fan-out/drop/retained/threadsafe/seq | 9.2 | ✅ | ✅ | bus.py:75-148; test_bus_and_worker.py:20-71 |
| ThrottledEmitter | 9.3 | ✅ | ✅ | bus.py:151-202; test_bus_and_worker.py:74-87 |
| RunWriter single-writer + DataPointer | 10.1 | ✅ | ✅ | writer.py:117-238; test_e2e_sim.py:93-98 |
| SWMR reader access | 10.1 | ❌ | — | writer.py:202 |
| Parquet compact at finalize | 10.2 | ✅ | ⚠️ silently skipped (no pyarrow) | writer.py:252-279; test_e2e_sim.py:104-106 |
| ArrowMetricsWriter | 10.2 | ❌ (optional) | — | — |
| Mask uint16 + recipe + spot-check | 10.3 | ◑ | ◑ | tpa_multiplier.py:60-71; test_e2e_sim.py:96 |
| Dual timestamps | 10.4 | ✅ (events/metrics), ◑ (checkpoints) | ◑ | contracts.py:68-75; task_manager.py:106 |
| §10.5 reproducibility checklist | 10.5 | ✅ (minus code_version) | ✅ | task_manager.py:375-406; test_e2e_sim.py:83-106 |
| grid_scan | 11.2 | ✅ | ✅ | sweep.py:31-53; test_e2e_sim.py:119-128 |
| adaptive_scan | 11.2 | ❌ (deferred by spec) | — | refactor.md:1170 |
| LoopThread 3-tier threading | 12.1 | ✅ | ❌ | bootstrap.py:79-108 |
| BlockingDeviceWorker + pool + pump | 12.3-12.4 | ✅ | ✅ (pump branch ❌) | worker.py:47-201; test_bus_and_worker.py:90-134 |
| Qt bridge / UI shell | 12.5/13.2 | ✅ | ❌ | ui/bridge.py; ui/main_window.py:336 |
| Gateway builtins pause/resume/cancel | 13.1 | ✅ | ◑ (cancel via gateway; pause/resume via TM) | gateway.py:57-77; test_e2e_sim.py:147-163 |
| TranscriptReplayTransport (L2) | 14.1 | ✅ | ❌ (no transcripts) | mock.py:75-120; README.md:67 |
| Sim physics closed loop (L4) | 14.2-14.3 | ✅ | ✅ | sim/context.py:34-48; test_e2e_sim.py:180-201 |
| CI gate | 14.3 | ❌ | — | no .github/, no CI config at repo root |
| Unified error model | 15 | ✅ | ◑ (mapping paths in transports untested) | errors.py:10-94; tcp.py:94-101 |
| raw-scpi diag capability | 15 | ❌ (spec: conditional) | — | refactor.md:1378 |
| import-linter layering | 18.13 | ❌ | — | pyproject.toml:27-29 |

**Known backlog explicitly deferred by spec/README** (not defects): adaptive_scan (refactor.md:1170), ArrowMetricsWriter (refactor.md:1078), gRPC/Tauri externalization (Phase 3, refactor.md:1412), per-device daemons (Phase 4), raw-scpi diagnostic capability (refactor.md:1378), legacy GUI specialty panels and optimization/analysis scripts (README.md:63-66), L2 transcript recording (README.md:67). Notably the implementation is *ahead* of the phase plan: Suspender, sweep helper, queue policy and L4 sim — all Phase-2 items — already exist in code; what's missing is their verification, not their existence.