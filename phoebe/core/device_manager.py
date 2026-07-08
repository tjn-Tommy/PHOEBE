"""DeviceManager: instance registry, identity verification, health, leases
(refactor.md §5.4, §6).

It manages connected, identity-verified controller instances and owns the
lease ownership table.  It must NOT implement instrument actions — no
``acquire_trace()`` here, ever (no God Object).
"""
from __future__ import annotations

import asyncio
import time
from typing import Iterable, Sequence

from loguru import logger

from .bus import EventBus
from .config import AppConfig, InstrumentConfig
from .contracts import InstrumentId, TaskId, timestamps
from .controller import InstrumentController, InstrumentSnapshot
from .di import ResolvedRequirement
from .errors import (
    InstrumentConnectionError,
    InstrumentError,
    LeaseUnavailableError,
)
from .events import DeviceHealthEvent, ErrorEvent
from .factory import AppDependencies, ControllerFactoryRegistry
from .lease import Lease, LeaseSet, make_lease


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
        self._owners: dict[InstrumentId, Lease] = {}      # ownership table
        self._lease_sets: dict[TaskId, LeaseSet] = {}
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ build
    async def start(self, *, connect: bool = True) -> None:
        """Create all configured controllers, connect and verify identity."""
        for cfg in self._config.instruments:
            controller = self._factories.create(cfg, self._deps)
            self._controllers[cfg.instrument_id] = controller
        if connect:
            for cfg in self._config.instruments:
                await self.connect_instrument(cfg.instrument_id)

    async def connect_instrument(self, instrument_id: InstrumentId) -> None:
        controller = self._controllers[instrument_id]
        cfg = self._config.instrument(instrument_id)
        await controller.connect()
        await self._verify_identity(controller, cfg)
        self._publish_health(instrument_id, "ok", "connected")

    async def _verify_identity(self, controller: InstrumentController,
                               cfg: InstrumentConfig) -> None:
        """The config file is not the source of truth — the device is (§5.4)."""
        identity = await controller.get_identity()
        haystack = " ".join((identity.vendor, identity.model, identity.raw)).lower()
        for token in (cfg.vendor, cfg.model):
            if token and token.lower() not in haystack:
                await controller.disconnect()
                raise InstrumentConnectionError(
                    f"{cfg.instrument_id}: identity mismatch — configured "
                    f"{cfg.vendor}/{cfg.model} but device reports {identity.raw!r}",
                    instrument_id=str(cfg.instrument_id),
                )

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
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
            remaining = leases.decref(iid)
            if remaining == 0:
                self._owners.pop(iid, None)
        self._lease_sets.pop(task_id, None)

    def touch(self, leases: LeaseSet) -> None:
        """Lease heartbeat; called from RunContext.checkpoint() (§6.4)."""
        leases.touch_all(time.monotonic())

    def owner_of(self, instrument_id: InstrumentId) -> Lease | None:
        return self._owners.get(instrument_id)

    def active_lease_count(self) -> int:
        return len(self._owners)

    # ----------------------------------------------------------------- reaper
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
        for task_id, lease_set in list(self._lease_sets.items()):
            expired = lease_set.expired(now)
            if not expired:
                continue
            logger.error("task {} leases expired (missing heartbeat); reclaiming", task_id)
            self._emit_error(
                "LeaseExpired",
                f"task {task_id} stopped heartbeating; reclaiming its devices",
            )
            for lease in expired:
                controller = self._controllers.get(lease.instrument_id)
                if controller is not None:
                    try:
                        await controller.stop()
                        await controller.safe_state()
                    except Exception:
                        logger.exception("safe_state failed for {}", lease.instrument_id)
                self._owners.pop(lease.instrument_id, None)
            self._lease_sets.pop(task_id, None)

    # ----------------------------------------------------------------- health
    async def health_check_all(self) -> None:
        for iid, controller in self._controllers.items():
            try:
                health = await controller.get_health()
                self._publish_health(iid, health.status, health.detail, health.metrics)
            except InstrumentError as exc:
                self._publish_health(iid, "error", str(exc))

    def _publish_health(self, instrument_id: InstrumentId, status: str,
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
