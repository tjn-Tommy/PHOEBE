"""Composition root: wire config → factories → managers → gateway
(refactor.md §16).

``build_runtime`` must run inside the asyncio loop that will own the system
(the dedicated loop thread in a Qt deployment, or the test loop in CI).
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from ..core.bus import EventBus
from ..core.config import AppConfig
from ..core.device_manager import DeviceManager
from ..core.factory import AppDependencies, ControllerFactoryRegistry
from ..core.gateway import Gateway
from ..core.plugin import PluginRegistry, plugin_registry
from ..core.task_manager import TaskManager
from ..core.worker import WorkerPool
from ..instruments.registry import register_builtin_factories


@dataclass
class AppRuntime:
    config: AppConfig
    bus: EventBus
    worker_pool: WorkerPool
    factories: ControllerFactoryRegistry
    device_manager: DeviceManager
    task_manager: TaskManager
    gateway: Gateway
    loop: asyncio.AbstractEventLoop

    async def shutdown(self) -> None:
        await self.task_manager.stop_suspenders()
        await self.device_manager.shutdown()
        self.worker_pool.stop_all()


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

    worker_pool = WorkerPool()
    factories = ControllerFactoryRegistry()
    register_builtin_factories(factories)

    deps = AppDependencies(worker_pool=worker_pool, bus=bus, app_config=config)
    device_manager = DeviceManager(config, factories, deps, bus=bus)
    await device_manager.start(connect=connect)
    if start_reaper:
        device_manager.start_reaper()

    task_manager = TaskManager(
        app_config=config, device_manager=device_manager, bus=bus,
        registry=plugins or plugin_registry, runs_root=runs_root,
    )
    task_manager.start_suspenders()
    gateway = Gateway(task_manager)

    return AppRuntime(
        config=config, bus=bus, worker_pool=worker_pool, factories=factories,
        device_manager=device_manager, task_manager=task_manager,
        gateway=gateway, loop=loop,
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
