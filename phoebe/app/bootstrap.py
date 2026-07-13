"""Composition root: wire config → factories → managers → gateway → services
(refactor.md §16; evolution plan Phase C).

``build_runtime`` must run inside the asyncio loop that will own the system
(the dedicated loop thread in a Qt deployment, or the test loop in CI).

Startup order matters: the run catalog + recovery scan run BEFORE any device
is connected — recovery is files-only by design (plan §6.2), and a crashed
run must be explained before new work is admitted.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ..core.bus import EventBus
from ..core.catalog import CATALOG_FILENAME, RunCatalog
from ..core.command_ledger import LEDGER_FILENAME, CommandLedger
from ..core.config import AppConfig
from ..core.device_manager import DeviceManager
from ..core.factory import AppDependencies, ControllerFactoryRegistry
from ..core.gateway import Gateway
from ..core.journal import scan_and_recover
from ..core.log_bridge import attach_log_bridge
from ..core.plugin import PluginRegistry, load_plugin_directory, plugin_registry
from ..core.task_manager import TaskManager
from ..core.worker import WorkerPool
from ..instruments.registry import register_builtin_factories
from ..services import (
    ConfigService,
    DeviceService,
    EventService,
    PluginService,
    RunService,
    ServiceHub,
)


@dataclass
class AppRuntime:
    config: AppConfig
    bus: EventBus
    worker_pool: WorkerPool
    factories: ControllerFactoryRegistry
    device_manager: DeviceManager
    task_manager: TaskManager
    gateway: Gateway
    services: ServiceHub
    catalog: RunCatalog
    command_ledger: CommandLedger
    loop: asyncio.AbstractEventLoop
    log_sink_id: int | None = None

    async def shutdown(self, *, deadline_s: float = 20.0) -> None:
        """Controlled shutdown (C2): drain active runs BEFORE tearing down
        devices and workers, so every run's cleanup (stop/safe_state, writer
        flush, lease release, final event) actually executes."""
        await self.task_manager.shutdown(deadline_s=deadline_s)
        await self.device_manager.shutdown()
        self.worker_pool.stop_all()
        if self.log_sink_id is not None:
            logger.remove(self.log_sink_id)
            self.log_sink_id = None
        self.catalog.close()
        self.command_ledger.close()


async def build_runtime(
    config: AppConfig,
    *,
    plugins: PluginRegistry | None = None,
    runs_root: Path | None = None,
    connect: bool = True,
    start_reaper: bool = True,
) -> AppRuntime:
    loop = asyncio.get_running_loop()
    bus = EventBus(default_queue_size=config.bus_default_queue_size,
                   dev_mode=config.mode == "dev")
    bus.bind_loop(loop)
    log_sink_id = attach_log_bridge(bus)

    # persisted truth first: catalog + ledger open, crashed runs explained
    # (files only — zero device I/O) before anything can be admitted.
    # State DBs live under .phoebe/ so runs_root itself stays runs-only.
    effective_runs_root = runs_root or Path(config.storage.runs_root)
    state_dir = effective_runs_root / ".phoebe"
    catalog = RunCatalog(state_dir / CATALOG_FILENAME)
    command_ledger = CommandLedger(state_dir / LEDGER_FILENAME)
    for report in scan_and_recover(effective_runs_root, catalog=catalog):
        logger.warning("startup recovery: run {} → {} ({})",
                       report.run_id, report.resolution, report.explanation)

    worker_pool = WorkerPool()
    factories = ControllerFactoryRegistry()
    register_builtin_factories(factories)

    deps = AppDependencies(worker_pool=worker_pool, bus=bus, app_config=config)
    device_manager = DeviceManager(config, factories, deps, bus=bus)
    await device_manager.start(connect=connect)
    if start_reaper:
        device_manager.start_reaper()
    if connect and config.health_poll_interval_s > 0:
        device_manager.start_health_poller(interval_s=config.health_poll_interval_s)

    registry = plugins or plugin_registry
    for plugin_dir in config.plugin_dirs:      # discovery degrades, never aborts
        load_plugin_directory(plugin_dir, registry)
    for failure in registry.failures():
        logger.warning("plugin {} unavailable ({}): {}", failure.plugin_id,
                       failure.source,
                       failure.error.message if failure.error else "?")
    task_manager = TaskManager(
        app_config=config, device_manager=device_manager, bus=bus,
        registry=registry, runs_root=runs_root,
        command_ledger=command_ledger, catalog=catalog,
        # a READY older than 3 poll intervals means the poller stopped
        # confirming it — admission rejects with HEALTH_STALE (plan §6.4)
        health_stale_after_s=(3 * config.health_poll_interval_s
                              if config.health_poll_interval_s > 0 else None),
    )
    task_manager.start_suspenders()
    gateway = Gateway(task_manager)

    services = ServiceHub(
        runs=RunService(gateway=gateway, task_manager=task_manager,
                        catalog=catalog, runs_root=effective_runs_root),
        devices=DeviceService(device_manager=device_manager, app_config=config),
        events=EventService(bus=bus),
        plugins=PluginService(registry=registry),
        config=ConfigService(app_config=config),
        loop=loop,
    )

    return AppRuntime(
        config=config, bus=bus, worker_pool=worker_pool, factories=factories,
        device_manager=device_manager, task_manager=task_manager,
        gateway=gateway, services=services, catalog=catalog,
        command_ledger=command_ledger, loop=loop, log_sink_id=log_sink_id,
    )


class LoopThread:
    """Dedicated asyncio loop thread (refactor.md §12.1).

    The Qt main thread stays UI-only; everything phoebe runs here.  Qt-side
    code submits commands via ``runtime.gateway.submit_threadsafe(env, loop)``
    and receives events through a UiEventBridge subscribed on this loop.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="phoebe-loop",
                                        daemon=True)
        self._started = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._started.set)
        self.loop.run_forever()

    def run_coroutine(self, coro):
        """Schedule a coroutine onto the loop; returns a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
