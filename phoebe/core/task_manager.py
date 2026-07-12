"""TaskManager: run state machine, checkpoint pause/cancel, suspender, cleanup
(refactor.md §8; hardened per PHOEBE_EVOLUTION_PLAN Phases A + C).

Every run's lifecycle::

    dispatch → typed admission chain (§6.4) → try_acquire_all (atomic)
      → PREPARING: run dir / journal / sink / writer / manifest / baseline_pre
        / stage()
      → RUNNING: plugin loop (checkpoint = pause/cancel/heartbeat point)
      → FINALIZING: stop() → [safe_state() on failure] → unstage()
        → writer close → baseline_post → release leases → remove log sink
      → terminal state, re-broadcast with final=True after cleanup

All setup runs inside try/finally so every failure moment reaches the same
cleanup; the RunJournal persists every lifecycle fact (execution outcome and
cleanup finalization are independent axes — plan §6.2);
``shutdown(deadline_s=...)`` drains active runs before the loop stops.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

from ..contracts.commands import AckCode, ack_from_decision
from ..contracts.run import JournalRecordType, RunManifest
from .admission import AdmissionChain, AdmissionContext
from .bus import EventBus, ThrottledEmitter
from .capability import CancellationToken
from .catalog import RunCatalog
from .command_ledger import CommandLedger
from .config import AppConfig, SuspenderConfig, config_hash
from .contracts import InstrumentId, RunId, TaskId, timestamps
from .controller import InstrumentController
from .device_manager import DeviceManager
from .di import DependencyResolver, ResolvedRequirement
from .errors import (
    CancelledByUser,
    DeviceNotReadyError,
    LeaseUnavailableError,
    error_code_of,
)
from .events import (
    DataPointerEvent,
    ErrorEvent,
    PreviewPayload,
    ProgressEvent,
    RunState,
    RunStateEvent,
)
from .gateway import CommandAck, CommandEnvelope
from .journal import RunJournal
from .lease import LeaseSet
from .plugin import PluginRegistry, PluginSpec, plugin_registry
from .writer import DataPointer, RunWriter, git_state, new_run_dir, write_json

# Total FSM (§6.2 of the evolution plan): every active state has a legal path
# to FINALIZING so no failure moment can strand a record in a non-terminal
# state; terminal outcomes are only ever reached through FINALIZING (except a
# cancel while still QUEUED, which held no resources to clean up).
_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.PREPARING, RunState.ABORTED}),
    RunState.PREPARING: frozenset({RunState.RUNNING, RunState.STOPPING,
                                   RunState.FINALIZING}),
    RunState.RUNNING: frozenset({RunState.PAUSING, RunState.STOPPING,
                                 RunState.FINALIZING}),
    RunState.PAUSING: frozenset({RunState.PAUSED, RunState.STOPPING,
                                 RunState.FINALIZING}),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.STOPPING,
                                RunState.FINALIZING}),
    RunState.STOPPING: frozenset({RunState.FINALIZING}),
    RunState.FINALIZING: frozenset({RunState.COMPLETED, RunState.FAILED,
                                    RunState.ABORTED}),
    # terminal states have no out-edges
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.ABORTED: frozenset(),
}

#: Terminal records kept in memory for late state_of()/wait() queries; older
#: ones are evicted so an always-on deployment doesn't grow without bound.
_MAX_TERMINAL_RECORDS = 256


class DispatchPolicy(StrEnum):
    REJECT = "reject"      # interactive default: 423 immediately
    QUEUE = "queue"        # overnight batches: FIFO


def new_task_id() -> TaskId:
    return TaskId(f"task_{uuid.uuid4().hex[:8]}")


class RunContext:
    """Aggregated framework services injected into every plugin entrypoint.

    Satisfies the InvocationContext protocol, so it can be passed straight to
    controller capability methods as ``context=ctx``.
    """

    def __init__(
        self,
        *,
        task_id: TaskId,
        run_id: RunId,
        run_dir: Path,
        writer: RunWriter,
        leases: LeaseSet,
        bus: EventBus,
        device_manager: DeviceManager,
        record: _RunRecord,
        log: Any,
        progress_interval_s: float = 0.1,
        pause_heartbeat_s: float = 60.0,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.run_dir = run_dir
        self.writer = writer
        self.leases = leases
        self.log = log
        self.cancel_token = record.cancel_token
        self._bus = bus
        self._dm = device_manager
        self._record = record
        self._throttle = ThrottledEmitter(bus, progress_interval_s)
        self._pause_heartbeat_s = pause_heartbeat_s

    # ---- InvocationContext protocol -----------------------------------------
    def ensure_not_cancelled(self) -> None:
        self.cancel_token.raise_if_cancelled()

    # ---- checkpoint: pause / cancel / heartbeat / yield (§8.3) --------------
    async def checkpoint(self, name: str, **state: float | int | str) -> None:
        self.log.debug("checkpoint {}", name, **({"cp_state": state} if state else {}))
        self._dm.touch(self.leases)                        # lease heartbeat (§6.4)
        record = self._record
        # A cancel that raced the pause request wins: never enter the pause
        # branch (and its PAUSED transition) once the token is set (H1).
        if record.pause_requested.is_set() and not self.cancel_token.cancelled:
            record.set_state(RunState.PAUSED)
            record.resume_event.clear()
            # Keep heartbeating while paused so an hours-long suspension is
            # never mistaken for a crashed holder and reaped (H5).
            while not record.resume_event.is_set():
                try:
                    await asyncio.wait_for(record.resume_event.wait(),
                                           timeout=self._pause_heartbeat_s)
                except TimeoutError:
                    self._dm.touch(self.leases)
            if not self.cancel_token.cancelled:
                record.set_state(RunState.RUNNING)
        self.cancel_token.raise_if_cancelled()
        await asyncio.sleep(0)                             # yield the loop

    # ---- observation emission --------------------------------------------------
    def emit_progress(self, *, step: int, total: int | None = None,
                      metrics: dict[str, float] | None = None,
                      pointer: DataPointer | None = None,
                      preview: PreviewPayload | None = None) -> None:
        self._throttle.emit(ProgressEvent(
            task_id=self.task_id, step=step, total=total,
            metrics=metrics or {}, **timestamps(),
        ))
        if pointer is not None:
            self._bus.publish(DataPointerEvent(
                task_id=self.task_id, run_id=pointer.run_id,
                dataset=pointer.dataset, index=pointer.index,
                preview=preview, **timestamps(),
            ))

    def flush_events(self) -> None:
        self._throttle.flush_all()


class _RunRecord:
    def __init__(self, task_id: TaskId, spec: PluginSpec, bus: EventBus) -> None:
        self.task_id = task_id
        self.spec = spec
        self.state = RunState.QUEUED
        self.cancel_token = CancellationToken()
        self.pause_requested = asyncio.Event()
        self.resume_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.auto_suspended = False        # set by Suspender, cleared on resume
        self.writer_failure: BaseException | None = None   # set on writer death (C1)
        self.leases_lost = False           # set when the reaper reclaimed our devices
        self.external_cancel_reason: str | None = None     # reaper / shutdown context
        self._bus = bus

    def set_state(self, new: RunState, *, reason: str | None = None,
                  final: bool = False) -> None:
        """``final=True`` only on a terminal state whose cleanup is already
        complete (nothing held) — otherwise the terminal event is re-broadcast
        with final=True after cleanup (plan §6.2)."""
        if new is self.state:
            return
        if new not in _TRANSITIONS[self.state]:
            raise RuntimeError(f"illegal run-state transition {self.state} → {new}")
        self.state = new
        self._bus.publish(RunStateEvent(
            task_id=self.task_id, state=new, reason=reason, final=final,
            **timestamps(),
        ))


class _PendingRun:
    def __init__(self, envelope: CommandEnvelope, spec: PluginSpec,
                 config: Any, reqs: list[ResolvedRequirement], task_id: TaskId) -> None:
        self.envelope = envelope
        self.spec = spec
        self.config = config
        self.reqs = reqs
        self.task_id = task_id


class TaskManager:
    def __init__(
        self,
        *,
        app_config: AppConfig,
        device_manager: DeviceManager,
        bus: EventBus,
        registry: PluginRegistry | None = None,
        runs_root: Path | None = None,
        command_ledger: CommandLedger | None = None,
        catalog: RunCatalog | None = None,
        health_stale_after_s: float | None = None,
    ) -> None:
        self._config = app_config
        self._dm = device_manager
        self._bus = bus
        self._registry = registry or plugin_registry
        self._runs_root = runs_root or Path(app_config.storage.runs_root)
        self._policy = DispatchPolicy(app_config.dispatch_policy)
        self._ledger = command_ledger
        self._catalog = catalog
        self._records: dict[TaskId, _RunRecord] = {}
        self._queue: list[_PendingRun] = []
        self._suspenders: list[Suspender] = []
        self._app_config_hash = config_hash(app_config)
        self._bg_tasks: set[asyncio.Task] = set()          # strong refs (asyncio GC hazard)
        self._maintenance_reason: str | None = None
        # heartbeat cadence for paused checkpoints: comfortably inside the TTL
        self._pause_heartbeat_s = max(0.05, min(app_config.lease_ttl_s / 3.0, 60.0))
        self._admission = AdmissionChain(
            registry=self._registry,
            make_resolver=self._make_resolver,
            ledger=command_ledger,
            maintenance_reason=lambda: self._maintenance_reason,
            lifecycle_state=device_manager.lifecycle_state,
            health_age_s=device_manager.health_age_s,
            health_stale_after_s=health_stale_after_s,
        )
        device_manager.set_reap_hooks(on_reaped=self._on_leases_reaped,
                                      after_reap=self._maybe_start_next_queued)

    # ------------------------------------------------------------- dispatch
    async def dispatch(self, cmd: CommandEnvelope) -> CommandAck:
        """Dispatch v2 (plan §6.4): the typed admission chain decides; only
        the lease/queue step lives here because admitting and creating the run
        must be atomic on the loop."""
        ctx = AdmissionContext(envelope=cmd)
        decision = self._admission.run(ctx)
        if decision is not None:
            if ctx.replayed_ack is not None:               # idempotent resubmit
                return ctx.replayed_ack
            return ack_from_decision(cmd.command_id, decision)
        spec = ctx.spec
        assert spec is not None                            # chain guarantees

        task_id = new_task_id()
        try:
            leases = self._dm.try_acquire_all(task_id, ctx.requirements)
        except DeviceNotReadyError as e:
            # Defense-in-depth re-check (the chain already gated on cached
            # lifecycle state): reject instead of queueing on a device that
            # may never come back (plan §6.3: leases legal only in READY).
            return CommandAck(command_id=cmd.command_id, accepted=False,
                              code=AckCode.DEVICE_NOT_READY,
                              reason=f"device not ready: {e}")
        except LeaseUnavailableError as e:
            if self._policy is DispatchPolicy.REJECT:
                return CommandAck(command_id=cmd.command_id, accepted=False,
                                  code=AckCode.DEVICE_BUSY,
                                  reason=f"locked by {e.holder}")
            record = _RunRecord(task_id, spec, self._bus)
            self._records[task_id] = record
            self._queue.append(_PendingRun(cmd, spec, ctx.config,
                                           ctx.requirements, task_id))
            self._bus.publish(RunStateEvent(task_id=task_id, state=RunState.QUEUED,
                                            reason="waiting for devices", **timestamps()))
            ack = CommandAck(command_id=cmd.command_id, accepted=True,
                             code=AckCode.QUEUED, task_id=task_id, queued=True)
            self._ledger_record(cmd, ack)
            return ack

        record = _RunRecord(task_id, spec, self._bus)
        self._records[task_id] = record
        self._start_run(record, ctx.entrypoint, ctx.config, ctx.requirements,
                        leases, cmd)
        ack = CommandAck(command_id=cmd.command_id, accepted=True,
                         code=AckCode.ACCEPTED, task_id=task_id)
        self._ledger_record(cmd, ack)
        return ack

    def _ledger_record(self, cmd: CommandEnvelope, ack: CommandAck) -> None:
        """Persist the first ack of an accepted command; a ledger failure must
        never fail the dispatch it audits."""
        if self._ledger is None:
            return
        try:
            self._ledger.record(cmd, ack)
        except Exception:
            logger.exception("command ledger record for {} failed", cmd.command_id)

    def _make_resolver(self) -> DependencyResolver:
        """Built per dispatch so DI reads the LIVE inventory (plan §6.3) —
        a device added/recovered after startup is immediately resolvable."""
        return DependencyResolver(
            role_map=self._dm.role_map(),
            kind_index=self._dm.kind_index(),
            plugin_bindings=self._config.plugin_bindings,
        )

    def _start_run(self, record: _RunRecord, entrypoint: Any, config: Any,
                   reqs: list[ResolvedRequirement], leases: LeaseSet,
                   cmd: CommandEnvelope) -> None:
        record.task = asyncio.create_task(
            self._execute(record, entrypoint, config, reqs, leases, cmd),
            name=f"run-{record.task_id}",
        )

    # ----------------------------------------------------------- built-ins
    def request_pause(self, task_id: TaskId) -> None:
        record = self._records[task_id]
        record.set_state(RunState.PAUSING, reason="pause requested")
        record.pause_requested.set()

    def request_resume(self, task_id: TaskId) -> None:
        record = self._records[task_id]
        if record.state is not RunState.PAUSED:
            raise RuntimeError(f"cannot resume from {record.state}")
        record.pause_requested.clear()
        record.auto_suspended = False
        record.resume_event.set()

    def request_cancel(self, task_id: TaskId) -> None:
        record = self._records[task_id]
        if record.state is RunState.QUEUED:
            self._queue = [p for p in self._queue if p.task_id != task_id]
            # a queued run held no resources — this terminal event IS final
            record.set_state(RunState.ABORTED, reason="cancelled while queued",
                             final=True)
            self._evict_terminal_records()
            return
        if (record.state.is_terminal or record.state is RunState.STOPPING
                or record.state is RunState.FINALIZING):
            return
        record.set_state(RunState.STOPPING, reason="cancel requested")
        record.pause_requested.clear()     # cancel wins over a pending pause (H1)
        record.cancel_token.cancel("cancelled by user")
        record.resume_event.set()          # unblock a paused checkpoint
        # hardware-side cancellation: stop() may bypass the operation lock
        for controller in self._dm.controllers_leased_by(task_id):
            self._spawn_bg(self._safe_stop(controller),
                           name=f"stop-{controller.instrument_id}")

    def _spawn_bg(self, coro: Any, *, name: str) -> asyncio.Task:
        """Fire-and-forget with a strong reference + logged exceptions —
        the house rule for background tasks (asyncio GC hazard)."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_done)
        return task

    def _bg_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error(
                "background task {} failed", task.get_name())

    def _on_leases_reaped(self, task_id: TaskId) -> None:
        """Reaper hook (H4): the run lost its devices — it must not keep
        executing against them, and its cleanup must not touch them either."""
        record = self._records.get(task_id)
        if record is None or record.state.is_terminal:
            return
        record.leases_lost = True
        record.external_cancel_reason = "leases reaped after missed heartbeats"
        record.cancel_token.cancel(record.external_cancel_reason)
        record.resume_event.set()
        if record.task is not None and not record.task.done():
            record.task.cancel()

    @staticmethod
    async def _safe_stop(controller: InstrumentController) -> None:
        try:
            await controller.stop()
        except Exception:
            logger.exception("stop() failed for {}", controller.instrument_id)

    # ------------------------------------------------------------- queries
    def state_of(self, task_id: TaskId) -> RunState:
        return self._records[task_id].state

    def active_tasks(self) -> tuple[TaskId, ...]:
        return tuple(tid for tid, r in self._records.items() if not r.state.is_terminal)

    async def wait(self, task_id: TaskId) -> RunState:
        record = self._records[task_id]
        if record.task is not None:
            try:
                await asyncio.shield(record.task)
            except asyncio.CancelledError:
                if not record.task.cancelled():
                    raise                  # the waiter itself was cancelled
        return record.state

    # -------------------------------------------------------------- execute
    async def _execute(self, record: _RunRecord, entrypoint: Any, config: Any,
                       reqs: list[ResolvedRequirement], leases: LeaseSet,
                       cmd: CommandEnvelope) -> None:
        """One run, end to end.  ALL setup runs inside the try (H3): any
        failure — run dir, log sink, writer, manifest, baseline, stage() —
        reaches the same FINALIZING cleanup path as a plugin failure."""
        task_id = record.task_id
        failed = False
        outcome = RunState.COMPLETED
        outcome_reason: str | None = None
        sink_id: int | None = None
        writer: RunWriter | None = None
        ctx: RunContext | None = None
        run_dir: Path | None = None
        journal: RunJournal | None = None
        cleanup_degraded = False
        controllers: list[InstrumentController] = []
        run_log = logger.bind(task_id=str(task_id), plugin=record.spec.plugin_id)

        def on_writer_failure(exc: BaseException) -> None:
            # C1: a dead writer must fail the run fast, not hang it — trip the
            # cancel token so checkpoints and controller waits bail out.
            record.writer_failure = exc
            record.cancel_token.cancel(f"writer failed: {exc}")
            record.resume_event.set()      # unblock a paused checkpoint

        try:
            record.set_state(RunState.PREPARING)
            run_id, run_dir = new_run_dir(self._runs_root,
                                          record.spec.plugin_id, task_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            journal = RunJournal(run_dir, task_id=task_id, run_id=run_id,
                                 catalog=self._catalog)
            if self._catalog is not None:
                try:
                    self._catalog.register_run(
                        run_id=str(run_id), task_id=str(task_id),
                        plugin_id=record.spec.plugin_id, command=cmd.command,
                        created_at=cmd.t_wall.isoformat(), run_dir=run_dir.name)
                except Exception:
                    run_log.exception("catalog registration failed")
            journal.record(JournalRecordType.ADMITTED,
                           detail=f"{cmd.command} by {cmd.issued_by}")
            journal.record(JournalRecordType.RUN_DIR_CREATED, detail=run_dir.name)
            sink_id = logger.add(                          # per-run sink; removed in finally (§8.6)
                run_dir / "experiment.jsonl", serialize=True, enqueue=True,
                level="DEBUG",
                filter=lambda r, tid=str(task_id): r["extra"].get("task_id") == tid,
            )
            run_log = logger.bind(task_id=str(task_id), run_id=str(run_id),
                                  plugin=record.spec.plugin_id)
            writer = RunWriter(
                run_id, run_dir,
                queue_size=self._config.storage.writer_queue_size,
                compact_parquet=self._config.storage.compact_metrics_to_parquet,
                on_failure=on_writer_failure,
                close_timeout_s=self._config.cleanup_timeout_s,
            )
            writer.start()
            ctx = RunContext(
                task_id=task_id, run_id=run_id, run_dir=run_dir, writer=writer,
                leases=leases, bus=self._bus, device_manager=self._dm,
                record=record, log=run_log,
                pause_heartbeat_s=self._pause_heartbeat_s,
            )
            controllers = [self._dm.controller(r.instrument_id) for r in reqs]
            injected = {r.param_name: self._dm.controller(r.instrument_id)
                        for r in reqs}
            await self._write_manifest(run_dir, run_id, record, cmd, config, reqs)
            await self._write_baseline(run_dir / "baseline_pre.json", leases)
            journal.record(JournalRecordType.BASELINE_CAPTURED)
            for controller in controllers:                 # stage (§4.4)
                record.cancel_token.raise_if_cancelled()
                await controller.stage()
            journal.record(JournalRecordType.STAGED)
            record.cancel_token.raise_if_cancelled()       # cancel during PREPARING
            record.set_state(RunState.RUNNING)
            journal.record(JournalRecordType.EXECUTION_STARTED)
            await entrypoint(config=config, ctx=ctx, **injected)
            if record.cancel_token.cancelled:
                raise CancelledByUser(record.cancel_token.reason or "cancelled")
        except CancelledByUser as exc:
            if record.writer_failure is not None:          # C1: writer death ≠ user cancel
                failed = True
                outcome = RunState.FAILED
                outcome_reason = f"writer failed: {record.writer_failure}"
                run_log.error("run failed: {}", outcome_reason)
                self._publish_error(task_id, record.writer_failure)
            elif record.leases_lost:
                failed = True
                outcome = RunState.FAILED
                outcome_reason = record.external_cancel_reason or "leases reaped"
                run_log.error("run failed: {}", outcome_reason)
            else:
                outcome = RunState.ABORTED
                outcome_reason = str(exc)
        except asyncio.CancelledError:                     # reaper / forced shutdown
            if record.leases_lost:
                failed = True
                outcome = RunState.FAILED
                outcome_reason = record.external_cancel_reason or "leases reaped"
            else:
                outcome = RunState.ABORTED
                outcome_reason = (record.external_cancel_reason
                                  or "run task cancelled")
            run_log.warning("run task cancelled: {}", outcome_reason)
        except Exception as exc:
            failed = True
            outcome = RunState.FAILED
            outcome_reason = str(exc)
            run_log.exception("run failed")
            self._publish_error(task_id, exc)
        finally:
            # Two persisted axes (plan §6.2): what the plugin did ...
            if journal is not None:
                journal.record(JournalRecordType.EXECUTION_OUTCOME,
                               outcome=outcome.value,  # type: ignore[arg-type]
                               detail=outcome_reason)
                journal.record(JournalRecordType.CLEANUP_STARTED)
            try:
                record.set_state(RunState.FINALIZING, reason=outcome_reason)
            except Exception:
                cleanup_degraded = True
                run_log.exception("FINALIZING transition failed")
            # Devices reclaimed by the reaper may already belong to another
            # run — never touch them from this run's cleanup (H4).
            if not record.leases_lost:
                try:
                    async with asyncio.timeout(self._config.cleanup_timeout_s):
                        for controller in reversed(controllers):
                            await controller.stop()
                            if failed:
                                await controller.safe_state()
                            await controller.unstage()
                except BaseException:
                    cleanup_degraded = True
                    run_log.exception("cleanup degraded")  # log but never re-raise
            if ctx is not None:
                ctx.flush_events()
            if writer is not None:
                try:
                    await writer.aclose()                  # flush the data plane (§10)
                    if journal is not None:
                        journal.record(JournalRecordType.WRITER_CLOSED)
                except BaseException:
                    cleanup_degraded = True
                    run_log.exception("writer close degraded")
            if run_dir is not None and not record.leases_lost:
                try:
                    await self._write_baseline(run_dir / "baseline_post.json", leases)
                except BaseException:
                    cleanup_degraded = True
                    run_log.exception("post baseline degraded")
            self._dm.release(task_id, leases)              # identity-checked (H4)
            if journal is not None:
                journal.record(JournalRecordType.LEASES_RELEASED)
            if sink_id is not None:
                logger.remove(sink_id)
            try:
                record.set_state(outcome, reason=outcome_reason)
            except Exception:
                cleanup_degraded = True
                run_log.exception("terminal transition failed")
            # ... and whether cleanup completed.  Leases lost to the reaper
            # mean this run could not clean up after itself — degraded.
            if journal is not None:
                journal.record(
                    JournalRecordType.FINALIZED,
                    finalized="degraded" if (cleanup_degraded
                                             or record.leases_lost) else "ok",
                    detail=outcome_reason)
                journal.close()
            self._bus.publish(RunStateEvent(                # terminal state re-broadcast
                task_id=task_id, state=record.state, reason=outcome_reason,
                final=True, **timestamps(),
            ))
            self._evict_terminal_records()
            self._maybe_start_next_queued()

    def _publish_error(self, task_id: TaskId, exc: BaseException) -> None:
        raw = getattr(exc, "instrument_id", None)
        self._bus.publish(ErrorEvent(
            task_id=task_id, error_type=type(exc).__name__, message=str(exc),
            code=error_code_of(exc),
            instrument_id=InstrumentId(raw) if isinstance(raw, str) else None,
            **timestamps(),
        ))

    def _evict_terminal_records(self) -> None:
        terminal = [tid for tid, r in self._records.items() if r.state.is_terminal]
        for tid in terminal[:max(0, len(terminal) - _MAX_TERMINAL_RECORDS)]:
            self._records.pop(tid, None)                   # oldest first (dict order)

    async def _write_manifest(self, run_dir: Path, run_id: RunId, record: _RunRecord,
                              cmd: CommandEnvelope, config: Any,
                              reqs: list[ResolvedRequirement]) -> None:
        commit, dirty = git_state(Path.cwd())
        config_json = config.model_dump_json()
        instruments: dict[str, dict[str, Any]] = {}
        for req in reqs:
            controller = self._dm.controller(req.instrument_id)
            icfg = self._config.instrument(req.instrument_id)
            try:
                identity = (await controller.get_identity()).model_dump()
            except Exception as exc:
                identity = {"error": str(exc)}
            instruments[str(req.instrument_id)] = {
                "kind": icfg.kind, "vendor": icfg.vendor, "model": icfg.model,
                "role": icfg.role, "backend": icfg.backend,
                "options": dict(icfg.options), "identity": identity,
            }
        manifest = RunManifest(
            run_id=run_id, task_id=record.task_id,
            plugin_id=record.spec.plugin_id, command=cmd.command,
            created_at=cmd.t_wall, config_json=config_json,
            config_hash=hashlib.sha256(config_json.encode()).hexdigest()[:16],
            app_config_hash=self._app_config_hash,
            git_commit=commit, git_dirty=dirty,
            instruments=instruments,
        )
        write_json(run_dir / "run.json", manifest)

    async def _write_baseline(self, path: Path, leases: LeaseSet) -> None:
        snapshots = await self._dm.snapshot_all(leases.instrument_ids())
        write_json(path, {k: v.model_dump() for k, v in snapshots.items()})

    def _maybe_start_next_queued(self) -> None:
        if self._maintenance_reason is not None:
            return
        for pending in list(self._queue):
            try:
                leases = self._dm.try_acquire_all(pending.task_id, pending.reqs)
            except (LeaseUnavailableError, DeviceNotReadyError):
                continue      # stays queued; retried when devices free/recover
            self._queue.remove(pending)
            record = self._records[pending.task_id]
            instance = pending.spec.instantiate()
            entrypoint = pending.spec.entrypoint(instance)
            self._start_run(record, entrypoint, pending.config,
                            pending.reqs, leases, pending.envelope)

    # ------------------------------------------------------------- shutdown
    def enter_maintenance(self, reason: str) -> None:
        """Reject all new plugin dispatches (built-ins still work)."""
        self._maintenance_reason = reason

    def exit_maintenance(self) -> None:
        self._maintenance_reason = None

    async def shutdown(self, *, deadline_s: float = 20.0) -> bool:
        """Controlled shutdown (C2): maintenance gate → cancel queued → cancel
        active → bounded wait → force-cancel stragglers → safe-state devices if
        degraded → stop suspenders.  Returns True when every run finalized its
        own cleanup within the deadline.

        Must run on the loop BEFORE the loop is stopped — otherwise run
        coroutines die mid-await and their cleanup never executes.
        """
        self.enter_maintenance("shutting down")
        for pending in list(self._queue):                  # queued: nothing to clean
            try:
                self.request_cancel(pending.task_id)
            except Exception:
                logger.exception("cancel of queued {} failed", pending.task_id)
        for tid in self.active_tasks():                    # active: cooperative cancel
            try:
                self.request_cancel(tid)
            except Exception:
                logger.exception("cancel of {} failed during shutdown", tid)
        tasks = [r.task for r in self._records.values()
                 if r.task is not None and not r.task.done()]
        clean = True
        if tasks:
            done, still_running = await asyncio.wait(tasks, timeout=deadline_s)
            if still_running:
                clean = False
                logger.error("{} run(s) missed the shutdown deadline; forcing",
                             len(still_running))
                for task in still_running:
                    task.cancel()
                await asyncio.wait(still_running,
                                   timeout=self._config.cleanup_timeout_s)
        if self._bg_tasks:                                 # in-flight stop() calls
            await asyncio.wait(self._bg_tasks, timeout=5.0)
        await self.stop_suspenders()
        if not clean:
            await self._dm.safe_all()                      # degraded: force-safe devices
        return clean

    # ----------------------------------------------------------- suspenders
    def start_suspenders(self) -> None:
        for cfg in self._config.suspenders:
            suspender = Suspender(cfg, self._bus, self)
            suspender.start()
            self._suspenders.append(suspender)

    async def stop_suspenders(self) -> None:
        for suspender in self._suspenders:
            await suspender.stop()
        self._suspenders.clear()


class Suspender:
    """Auto-suspend guard (refactor.md §8.4): metric out of range → pause all
    running tasks; back in range and stable for ``grace_s`` → resume the runs
    it suspended."""

    def __init__(self, config: SuspenderConfig, bus: EventBus, tm: TaskManager) -> None:
        self._cfg = config
        self._bus = bus
        self._tm = tm
        self._task: asyncio.Task | None = None
        self._back_in_range_since: float | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._watch(), name=f"suspender-{self._cfg.metric}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _out_of_range(self, value: float) -> bool:
        if self._cfg.min_value is not None and value < self._cfg.min_value:
            return True
        if self._cfg.max_value is not None and value > self._cfg.max_value:
            return True
        return False

    async def _watch(self) -> None:
        sub = self._bus.subscribe([self._cfg.watch_topic])
        try:
            async for event in sub:
                metrics = getattr(event, "metrics", None) or {}
                value = metrics.get(self._cfg.metric)
                if value is None:
                    continue
                loop_time = asyncio.get_running_loop().time()
                if self._out_of_range(value):
                    self._back_in_range_since = None
                    self._suspend_all()
                else:
                    if self._back_in_range_since is None:
                        self._back_in_range_since = loop_time
                    elif loop_time - self._back_in_range_since >= self._cfg.grace_s:
                        self._resume_suspended()
        finally:
            self._bus.unsubscribe(sub)

    def _suspend_all(self) -> None:
        for task_id in self._tm.active_tasks():
            record = self._tm._records[task_id]
            if record.state is RunState.RUNNING:
                logger.warning("suspender: {} out of range → pausing {}",
                               self._cfg.metric, task_id)
                self._tm.request_pause(task_id)
                record.auto_suspended = True

    def _resume_suspended(self) -> None:
        for task_id in self._tm.active_tasks():
            record = self._tm._records[task_id]
            if record.auto_suspended and record.state is RunState.PAUSED:
                logger.info("suspender: {} recovered → resuming {}",
                            self._cfg.metric, task_id)
                self._tm.request_resume(task_id)
