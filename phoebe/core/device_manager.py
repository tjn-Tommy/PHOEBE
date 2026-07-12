"""DeviceManager: instance registry, identity verification, health, leases
(refactor.md §5.4, §6).

It manages connected, identity-verified controller instances and owns the
lease ownership table.  It must NOT implement instrument actions — no
``acquire_trace()`` here, ever (no God Object).
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from loguru import logger

from .bus import EventBus
from .config import AppConfig, InstrumentConfig
from .contracts import InstrumentId, TaskId, timestamps
from .controller import ControllerStats, InstrumentController, InstrumentSnapshot
from .di import ResolvedRequirement
from .errors import (
    DeviceNotReadyError,
    InstrumentConnectionError,
    InstrumentError,
    LeaseUnavailableError,
)
from .events import DeviceHealthEvent, ErrorEvent
from .factory import AppDependencies, ControllerFactoryRegistry
from .lease import Lease, LeaseSet, make_lease
from .reconnect import HEALTH_STATUS_OF, DeviceLifecycleState, DeviceSupervisor


def _normalize_identity_token(token: str) -> str:
    """Lowercase and strip separators so configured tokens match vendor IDN
    punctuation (H7): ``rohde-schwarz`` ↔ ``ROHDE&SCHWARZ,RTO6,...``."""
    return re.sub(r"[^a-z0-9]", "", token.lower())


class DeviceManager:
    def __init__(
        self,
        config: AppConfig,
        factories: ControllerFactoryRegistry,
        deps: AppDependencies,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._factories = factories
        self._deps = deps
        self._bus = bus
        self._controllers: dict[InstrumentId, InstrumentController] = {}
        self._supervisors: dict[InstrumentId, DeviceSupervisor] = {}
        self._owners: dict[InstrumentId, Lease] = {}      # ownership table
        self._lease_sets: dict[TaskId, LeaseSet] = {}
        self._reaper_task: asyncio.Task | None = None
        self._health_poll_task: asyncio.Task | None = None
        # reap hooks (wired by TaskManager): cancel/mark the reaped run before
        # devices are safe-stated; wake the dispatch queue after reclamation —
        # also fired when a device recovers to READY, for the same reason.
        self._on_lease_reaped: Callable[[TaskId], None] | None = None
        self._after_reap: Callable[[], None] | None = None

    # ------------------------------------------------------------------ build
    async def start(self, *, connect: bool = True) -> None:
        """Create all configured controllers; connect through per-device
        supervisors.  Startup is degraded-tolerant (plan §6.3): a device that
        fails to connect boots in BACKOFF/ERROR/OFFLINE instead of aborting
        the app — the rest of the bench stays usable."""
        for cfg in self._config.instruments:
            controller = self._factories.create(cfg, self._deps)
            self._controllers[cfg.instrument_id] = controller
        if connect:
            await self.connect_all()

    async def connect_all(self) -> None:
        """Engage lifecycle supervision and attempt every device's first
        connect.  Never raises: failures land in BACKOFF (auto-retry with
        backoff), ERROR (fatal — operator must fix) or OFFLINE (gave up)."""
        for cfg in self._config.instruments:
            iid = cfg.instrument_id
            if iid not in self._supervisors:
                self._supervisors[iid] = DeviceSupervisor(
                    iid,
                    connect=lambda iid=iid: self.connect_instrument(iid),
                    disconnect=self._controllers[iid].disconnect,
                    settings=self._config.reconnect,
                    on_state_change=self._on_lifecycle_change,
                )
        for supervisor in self._supervisors.values():
            await supervisor.start()

    async def connect_instrument(self, instrument_id: InstrumentId) -> None:
        controller = self._controllers[instrument_id]
        cfg = self._config.instrument(instrument_id)
        await controller.connect()
        await self._verify_identity(controller, cfg)
        self._publish_health(instrument_id, "ok", "connected")

    async def _verify_identity(self, controller: InstrumentController,
                               cfg: InstrumentConfig) -> None:
        """The config file is not the source of truth — the device is (§5.4).

        Tokens are compared with separators stripped (H7): a configured
        ``rohde-schwarz`` must match a reported ``ROHDE&SCHWARZ``.  A mismatch
        is fatal — reconnecting cannot change what the device is."""
        identity = await controller.get_identity()
        haystack = _normalize_identity_token(
            " ".join((identity.vendor, identity.model, identity.raw)))
        for token in (cfg.vendor, cfg.model):
            if token and _normalize_identity_token(token) not in haystack:
                await controller.disconnect()
                raise InstrumentConnectionError(
                    f"{cfg.instrument_id}: identity mismatch — configured "
                    f"{cfg.vendor}/{cfg.model} but device reports {identity.raw!r}",
                    instrument_id=str(cfg.instrument_id), fatal=True,
                )

    # -------------------------------------------------------------- lifecycle
    def lifecycle_state(self, instrument_id: InstrumentId) -> DeviceLifecycleState:
        supervisor = self._supervisors.get(instrument_id)
        if supervisor is None:
            return DeviceLifecycleState.CONFIGURED
        return supervisor.state

    def supervisor(self, instrument_id: InstrumentId) -> DeviceSupervisor | None:
        return self._supervisors.get(instrument_id)

    def health_age_s(self, instrument_id: InstrumentId) -> float | None:
        """Seconds since the device last proved healthy; None when lifecycle
        supervision is not engaged or nothing was ever confirmed.  Cached
        state only — admission must never do device I/O (plan §6.4)."""
        supervisor = self._supervisors.get(instrument_id)
        if supervisor is None:
            return None
        return supervisor.health_age_s()

    async def disable_instrument(self, instrument_id: InstrumentId) -> None:
        supervisor = self._supervisors.get(instrument_id)
        if supervisor is not None:
            await supervisor.disable()

    async def reconnect_instrument(self, instrument_id: InstrumentId) -> bool:
        supervisor = self._supervisors.get(instrument_id)
        if supervisor is None:
            await self.connect_instrument(instrument_id)
            return True
        return await supervisor.reconnect_now()

    def _on_lifecycle_change(self, instrument_id: InstrumentId,
                             state: DeviceLifecycleState, detail: str | None) -> None:
        self._publish_health(instrument_id, HEALTH_STATUS_OF[state],
                             detail or state.value)
        if state is DeviceLifecycleState.READY and self._after_reap is not None:
            # queued runs waiting on this device can start now
            try:
                self._after_reap()
            except Exception:
                logger.exception("queue wake after READY failed")

    async def safe_all(self) -> None:
        """stop() + safe_state() every controller — used by degraded shutdown
        when active runs could not confirm their own cleanup in time."""
        for iid, controller in self._controllers.items():
            try:
                await controller.stop()
                await controller.safe_state()
            except Exception:
                logger.exception("safe_state failed for {}", iid)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        if self._health_poll_task is not None:
            self._health_poll_task.cancel()
            self._health_poll_task = None
        for supervisor in self._supervisors.values():
            await supervisor.stop()                       # cancel pending retries
        for controller in self._controllers.values():
            try:
                await controller.disconnect()
            except Exception:
                logger.exception("disconnect failed for {}", controller.instrument_id)

    # -------------------------------------------------------------- inventory
    def controller(self, instrument_id: InstrumentId) -> InstrumentController:
        return self._controllers[instrument_id]

    def controllers_of(self, leases: LeaseSet) -> list[InstrumentController]:
        return [self._controllers[iid] for iid in leases.instrument_ids()]

    def controllers_leased_by(self, task_id: TaskId) -> list[InstrumentController]:
        """Controllers currently leased to a task (empty when it holds none)."""
        lease_set = self._lease_sets.get(task_id)
        if lease_set is None:
            return []
        return self.controllers_of(lease_set)

    def inventory(self) -> tuple[InstrumentId, ...]:
        return tuple(self._controllers.keys())

    def kind_index(self) -> dict[str, tuple[InstrumentId, ...]]:
        index: dict[str, list[InstrumentId]] = {}
        for iid, controller in self._controllers.items():
            for kind in controller.descriptor.provides:
                index.setdefault(kind, []).append(iid)
        return {k: tuple(v) for k, v in index.items()}

    def role_map(self) -> dict[str, InstrumentId]:
        return self._config.role_map()

    async def snapshot_all(
        self, instrument_ids: Iterable[InstrumentId] | None = None
    ) -> dict[str, InstrumentSnapshot]:
        result: dict[str, InstrumentSnapshot] = {}
        ids = tuple(instrument_ids) if instrument_ids is not None else self.inventory()
        for iid in ids:
            try:
                result[str(iid)] = await self._controllers[iid].get_snapshot()
            except Exception as exc:
                logger.warning("snapshot failed for {}: {}", iid, exc)
        return result

    # ----------------------------------------------------------------- leases
    def try_acquire_all(
        self,
        task_id: TaskId,
        requirements: Sequence[ResolvedRequirement],
        parent: LeaseSet | None = None,
    ) -> LeaseSet:
        """Synchronous — no await inside → atomic on a single loop.

        Any unavailable device rolls back every lease granted in this call and
        raises; partial holds never wait for the remainder (no hold-and-wait),
        so deadlock is structurally impossible.
        """
        now = time.monotonic()
        # Lease acquisition is legal only in READY (plan §6.3).  CONFIGURED
        # means lifecycle supervision is not engaged (direct wiring in tests /
        # connect=False) and keeps the pre-lifecycle semantics.
        for req in requirements:
            state = self.lifecycle_state(req.instrument_id)
            if state not in (DeviceLifecycleState.READY,
                             DeviceLifecycleState.CONFIGURED):
                raise DeviceNotReadyError(str(req.instrument_id), state.value)
        granted: list[Lease] = []
        inherited: list[InstrumentId] = []
        try:
            for req in requirements:
                if parent is not None and parent.holds(req.instrument_id):
                    parent.incref(req.instrument_id)          # inheritance (§6.3)
                    inherited.append(req.instrument_id)
                    continue
                owner = self._owners.get(req.instrument_id)
                if owner is not None:
                    raise LeaseUnavailableError(
                        str(req.instrument_id), holder=str(owner.holder_task_id)
                    )
                lease = make_lease(
                    task_id, req.instrument_id,
                    ttl_s=self._config.lease_ttl_s,
                    parent=None,
                )
                self._owners[req.instrument_id] = lease
                granted.append(lease)
        except LeaseUnavailableError:
            for lease in granted:                              # roll back this call
                self._owners.pop(lease.instrument_id, None)
            if parent is not None:
                for iid in inherited:
                    parent.decref(iid)
            raise
        lease_set = LeaseSet.merge(parent, granted, now_mono=now)
        self._lease_sets[task_id] = lease_set
        return lease_set

    def release(self, task_id: TaskId, leases: LeaseSet) -> None:
        for iid in leases.instrument_ids():
            lease = leases.lease_for(iid)
            remaining = leases.decref(iid)
            if remaining == 0:
                # Identity check: after a reap + re-acquire, the ownership row
                # belongs to the new holder — never pop someone else's lease.
                owner = self._owners.get(iid)
                if owner is not None and owner.lease_id == lease.lease_id:
                    self._owners.pop(iid, None)
        if self._lease_sets.get(task_id) is leases:
            self._lease_sets.pop(task_id, None)

    def touch(self, leases: LeaseSet) -> None:
        """Lease heartbeat; called from RunContext.checkpoint() (§6.4)."""
        leases.touch_all(time.monotonic())

    def owner_of(self, instrument_id: InstrumentId) -> Lease | None:
        return self._owners.get(instrument_id)

    def active_lease_count(self) -> int:
        return len(self._owners)

    # ----------------------------------------------------------------- reaper
    def set_reap_hooks(self, *, on_reaped: Callable[[TaskId], None] | None = None,
                       after_reap: Callable[[], None] | None = None) -> None:
        """Wired by TaskManager: ``on_reaped(task_id)`` cancels/marks the run
        whose leases expired (called before its devices are safe-stated);
        ``after_reap()`` runs once per sweep after reclamation so queued runs
        waiting on the freed devices can start."""
        self._on_lease_reaped = on_reaped
        self._after_reap = after_reap

    def start_reaper(self, *, interval_s: float = 30.0) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(
                self._reap_loop(interval_s), name="lease-reaper"
            )

    async def _reap_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lease reaper iteration failed")

    async def _reap_once(self) -> None:
        now = time.monotonic()
        reaped_any = False
        for task_id, lease_set in list(self._lease_sets.items()):
            expired = lease_set.expired(now)
            if not expired:
                continue
            logger.error("task {} leases expired (missing heartbeat); reclaiming", task_id)
            self._emit_error(
                "LeaseExpired",
                f"task {task_id} stopped heartbeating; reclaiming its devices",
            )
            # Cancel/mark the run FIRST so it stops issuing operations against
            # devices it is about to lose.
            if self._on_lease_reaped is not None:
                try:
                    self._on_lease_reaped(task_id)
                except Exception:
                    logger.exception("on_reaped hook failed for {}", task_id)
            for lease in expired:
                controller = self._controllers.get(lease.instrument_id)
                if controller is not None:
                    try:
                        await controller.stop()
                        await controller.safe_state()
                    except Exception:
                        logger.exception("safe_state failed for {}", lease.instrument_id)
                owner = self._owners.get(lease.instrument_id)
                if owner is not None and owner.lease_id == lease.lease_id:
                    self._owners.pop(lease.instrument_id, None)
            if self._lease_sets.get(task_id) is lease_set:
                self._lease_sets.pop(task_id, None)
            reaped_any = True
        if reaped_any and self._after_reap is not None:
            try:
                self._after_reap()
            except Exception:
                logger.exception("after_reap hook failed")

    # ----------------------------------------------------------------- health
    async def health_check_all(self) -> None:
        for iid, controller in self._controllers.items():
            try:
                health = await controller.get_health()
                self._publish_health(iid, health.status, health.detail, health.metrics)
            except InstrumentError as exc:
                self._publish_health(iid, "error", str(exc))

    def stats_all(self) -> dict[str, ControllerStats]:
        """Operational stats per controller (plan §3.1 A2) for panels/API."""
        return {str(iid): c.get_stats() for iid, c in self._controllers.items()}

    def start_health_poller(self, *, interval_s: float) -> None:
        """Periodic per-device probes through the op-lock (plan §3.1 A10);
        results drive the READY ↔ DEGRADED lifecycle edges and the
        threshold-triggered handle rebuild."""
        if interval_s <= 0 or self._health_poll_task is not None:
            return
        self._health_poll_task = asyncio.create_task(
            self._health_poll_loop(interval_s), name="health-poller")

    async def _health_poll_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self._probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("health poll iteration failed")

    async def _probe_all(self) -> None:
        for iid, supervisor in self._supervisors.items():
            # BACKOFF/OFFLINE/ERROR devices are the reconnect loop's business
            if supervisor.state not in (DeviceLifecycleState.READY,
                                        DeviceLifecycleState.DEGRADED):
                continue
            controller = self._controllers[iid]
            try:
                health = await asyncio.wait_for(
                    controller.probe_health(),
                    timeout=self._config.reconnect.probe_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                controller.note_error(exc)
                self._publish_health(iid, "error", str(exc))
                await supervisor.note_probe_failure(
                    exc, allow_rebuild=self._owners.get(iid) is None)
                continue
            if health.status == "error":
                controller.note_error(health.detail or "probe reported error")
                self._publish_health(iid, "error", health.detail, health.metrics)
                await supervisor.note_probe_failure(
                    InstrumentError(health.detail or "probe reported error",
                                    instrument_id=str(iid)),
                    allow_rebuild=self._owners.get(iid) is None)
            else:
                controller.note_ok()
                self._publish_health(iid, health.status, health.detail, health.metrics)
                await supervisor.note_probe_ok()

    def _publish_health(self, instrument_id: InstrumentId,
                        status: Literal["ok", "degraded", "error", "offline"],
                        detail: str | None = None,
                        metrics: dict[str, float] | None = None) -> None:
        if self._bus is None:
            return
        self._bus.publish(DeviceHealthEvent(
            instrument_id=instrument_id, status=status, detail=detail,
            metrics=metrics or {}, **timestamps(),
        ))

    def _emit_error(self, error_type: str, message: str) -> None:
        if self._bus is None:
            return
        self._bus.publish(ErrorEvent(
            error_type=error_type, message=message, **timestamps(),
        ))
