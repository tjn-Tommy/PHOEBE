"""TaskManager: run state machine, checkpoint pause/cancel, suspender, cleanup
(refactor.md §8).

Every run's lifecycle::

    dispatch → validate payload → resolve DI → try_acquire_all (atomic)
      → run dir / manifest / baseline_pre → stage() → RUNNING
      → plugin loop (checkpoint = pause/cancel/heartbeat point)
      → finally: stop() → [safe_state() on failure] → unstage()
        → writer close → baseline_post → release leases → remove log sink
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

from .bus import EventBus, ThrottledEmitter
from .capability import CancellationToken
from .config import AppConfig, SuspenderConfig, config_hash
from .contracts import RunId, TaskId, timestamps, validate_boundary
from .controller import InstrumentController
from .device_manager import DeviceManager
from .di import DependencyResolver, ResolvedRequirement
from .errors import CancelledByUser, LeaseUnavailableError
from .events import (
    DataPointerEvent,
    ErrorEvent,
    ProgressEvent,
    RunState,
    RunStateEvent,
    TracePreview,
)
from .gateway import CommandAck, CommandEnvelope
from .lease import LeaseSet
from .plugin import PluginRegistry, PluginSpec, plugin_registry
from .writer import DataPointer, RunManifest, RunWriter, git_state, new_run_dir, write_json

_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.ABORTED}),
    RunState.RUNNING: frozenset({RunState.PAUSING, RunState.STOPPING,
                                 RunState.COMPLETED, RunState.FAILED}),
    RunState.PAUSING: frozenset({RunState.PAUSED, RunState.STOPPING}),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.STOPPING}),
    RunState.STOPPING: frozenset({RunState.ABORTED, RunState.FAILED}),
    # terminal states have no out-edges
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.ABORTED: frozenset(),
}


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
        record: "_RunRecord",
        log: Any,
        progress_interval_s: float = 0.1,
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

    # ---- InvocationContext protocol -----------------------------------------
    def ensure_not_cancelled(self) -> None:
        self.cancel_token.raise_if_cancelled()

    # ---- checkpoint: pause / cancel / heartbeat / yield (§8.3) --------------
    async def checkpoint(self, name: str, **state: float | int | str) -> None:
        self.log.debug("checkpoint {}", name, **({"cp_state": state} if state else {}))
        self._dm.touch(self.leases)                        # lease heartbeat (§6.4)
        record = self._record
        if record.pause_requested.is_set():
            record.set_state(RunState.PAUSED)
            record.resume_event.clear()
            await record.resume_event.wait()
            if not self.cancel_token.cancelled:
                record.set_state(RunState.RUNNING)
        self.cancel_token.raise_if_cancelled()
        await asyncio.sleep(0)                             # yield the loop

    # ---- observation emission --------------------------------------------------
    def emit_progress(self, *, step: int, total: int | None = None,
                      metrics: dict[str, float] | None = None,
                      pointer: DataPointer | None = None,
                      preview: TracePreview | None = None) -> None:
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
        self._bus = bus

    def set_state(self, new: RunState, *, reason: str | None = None) -> None:
        if new is self.state:
            return
        if new not in _TRANSITIONS[self.state]:
            raise RuntimeError(f"illegal run-state transition {self.state} → {new}")
        self.state = new
        self._bus.publish(RunStateEvent(
            task_id=self.task_id, state=new, reason=reason, **timestamps(),
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
    ) -> None:
        self._config = app_config
        self._dm = device_manager
        self._bus = bus
        self._registry = registry or plugin_registry
        self._runs_root = runs_root or Path(app_config.storage.runs_root)
        self._policy = DispatchPolicy(app_config.dispatch_policy)
        self._records: dict[TaskId, _RunRecord] = {}
        self._queue: list[_PendingRun] = []
        self._resolver = DependencyResolver(
            role_map=device_manager.role_map(),
            kind_index=device_manager.kind_index(),
            plugin_bindings=app_config.plugin_bindings,
        )
        self._suspenders: list[Suspender] = []
        self._app_config_hash = config_hash(app_config)

    # ------------------------------------------------------------- dispatch
    async def dispatch(self, cmd: CommandEnvelope) -> CommandAck:
        spec = self._registry.spec_for_command(cmd.command)
        if spec is None:
            return CommandAck(command_id=cmd.command_id, accepted=False,
                              reason=f"unknown command {cmd.command!r}")
        try:
            config = validate_boundary(spec.config_type, cmd.payload)  # contract choke point
        except Exception as exc:
            return CommandAck(command_id=cmd.command_id, accepted=False,
                              reason=f"invalid payload: {exc}")
        instance = spec.instantiate()
        entrypoint = spec.entrypoint(instance)
        try:
            reqs = self._resolver.resolve(spec.plugin_id, entrypoint)
        except Exception as exc:
            return CommandAck(command_id=cmd.command_id, accepted=False,
                              reason=str(exc))

        task_id = new_task_id()
        try:
            leases = self._dm.try_acquire_all(task_id, reqs)
        except LeaseUnavailableError as e:
            if self._policy is DispatchPolicy.REJECT:
                return CommandAck(command_id=cmd.command_id, accepted=False,
                                  reason=f"423 locked by {e.holder}")
            record = _RunRecord(task_id, spec, self._bus)
            self._records[task_id] = record
            self._queue.append(_PendingRun(cmd, spec, config, reqs, task_id))
            self._bus.publish(RunStateEvent(task_id=task_id, state=RunState.QUEUED,
                                            reason="waiting for devices", **timestamps()))
            return CommandAck(command_id=cmd.command_id, accepted=True,
                              task_id=task_id, queued=True)

        record = _RunRecord(task_id, spec, self._bus)
        self._records[task_id] = record
        self._start_run(record, entrypoint, config, reqs, leases, cmd)
        return CommandAck(command_id=cmd.command_id, accepted=True, task_id=task_id)

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
            record.set_state(RunState.ABORTED, reason="cancelled while queued")
            return
        if record.state.is_terminal or record.state is RunState.STOPPING:
            return
        record.set_state(RunState.STOPPING, reason="cancel requested")
        record.cancel_token.cancel("cancelled by user")
        record.resume_event.set()          # unblock a paused checkpoint
        # hardware-side cancellation: stop() may bypass the operation lock
        lease_set = self._dm._lease_sets.get(task_id)
        if lease_set is not None:
            for controller in self._dm.controllers_of(lease_set):
                asyncio.create_task(self._safe_stop(controller))

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
            await asyncio.shield(record.task)
        return record.state

    # -------------------------------------------------------------- execute
    async def _execute(self, record: _RunRecord, entrypoint: Any, config: Any,
                       reqs: list[ResolvedRequirement], leases: LeaseSet,
                       cmd: CommandEnvelope) -> None:
        task_id = record.task_id
        run_id, run_dir = new_run_dir(self._runs_root, record.spec.plugin_id, task_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        sink_id = logger.add(                              # per-run sink; removed in finally (§8.6)
            run_dir / "experiment.jsonl", serialize=True, enqueue=True, level="DEBUG",
            filter=lambda r, tid=str(task_id): r["extra"].get("task_id") == tid,
        )
        run_log = logger.bind(task_id=str(task_id), run_id=str(run_id),
                              plugin=record.spec.plugin_id)

        writer = RunWriter(
            run_id, run_dir,
            queue_size=self._config.storage.writer_queue_size,
            compact_parquet=self._config.storage.compact_metrics_to_parquet,
        )
        writer.start()
        ctx = RunContext(
            task_id=task_id, run_id=run_id, run_dir=run_dir, writer=writer,
            leases=leases, bus=self._bus, device_manager=self._dm,
            record=record, log=run_log,
        )
        controllers = [self._dm.controller(r.instrument_id) for r in reqs]
        injected = {r.param_name: self._dm.controller(r.instrument_id) for r in reqs}

        failed = False
        try:
            await self._write_manifest(run_dir, run_id, record, cmd, config, reqs)
            await self._write_baseline(run_dir / "baseline_pre.json", leases)
            for controller in controllers:                 # stage (§4.4)
                await controller.stage()
            record.set_state(RunState.RUNNING)
            await entrypoint(config=config, ctx=ctx, **injected)
            if record.cancel_token.cancelled:
                raise CancelledByUser(record.cancel_token.reason or "cancelled")
            record.set_state(RunState.COMPLETED)
        except CancelledByUser as exc:
            if record.state is not RunState.STOPPING:
                record.set_state(RunState.STOPPING, reason=str(exc))
            record.set_state(RunState.ABORTED, reason=str(exc))
        except Exception as exc:
            failed = True
            run_log.exception("run failed")
            self._bus.publish(ErrorEvent(
                task_id=task_id, error_type=type(exc).__name__,
                message=str(exc), **timestamps(),
            ))
            if record.state in (RunState.PAUSING, RunState.PAUSED):
                record.set_state(RunState.STOPPING, reason="run failed")
            record.set_state(RunState.FAILED, reason=str(exc))
        finally:
            try:
                async with asyncio.timeout(self._config.cleanup_timeout_s):
                    for controller in reversed(controllers):
                        await controller.stop()
                        if failed:
                            await controller.safe_state()
                        await controller.unstage()
            except Exception:
                run_log.exception("cleanup degraded")      # log but never re-raise
            ctx.flush_events()
            try:
                await writer.aclose()                      # flush the data plane (§10)
            except Exception:
                run_log.exception("writer close degraded")
            try:
                await self._write_baseline(run_dir / "baseline_post.json", leases)
            except Exception:
                run_log.exception("post baseline degraded")
            self._dm.release(task_id, leases)
            logger.remove(sink_id)
            self._bus.publish(RunStateEvent(                # terminal state re-broadcast
                task_id=task_id, state=record.state, reason="final", **timestamps(),
            ))
            self._maybe_start_next_queued()

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
        for pending in list(self._queue):
            try:
                leases = self._dm.try_acquire_all(pending.task_id, pending.reqs)
            except LeaseUnavailableError:
                continue
            self._queue.remove(pending)
            record = self._records[pending.task_id]
            instance = pending.spec.instantiate()
            entrypoint = pending.spec.entrypoint(instance)
            self._start_run(record, entrypoint, pending.config,
                            pending.reqs, leases, pending.envelope)

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
