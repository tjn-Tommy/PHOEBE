"""Per-device blocking worker threads (refactor.md §12.3–§12.4).

Blocking calls (PyVISA, sockets, vendor DLLs, NI-DAQmx) live exclusively on
one dedicated thread per device.  A shared pool would let one slow device
starve the others; a per-device thread also satisfies DLLs whose handles are
pinned to the creating thread.

For DLL devices that require a Win32 message pump (Santec SLM-200), pass
``pump=True``: the worker polls its job queue with a short timeout and pumps
pending window messages while idle, mirroring the vendor sample's UI thread.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, TypeVar
from collections.abc import Callable

from loguru import logger

T = TypeVar("T")

_STOP = object()


def _make_win32_message_pump() -> Callable[[], None]:
    """Return a callable that drains pending Win32 messages; no-op off Windows."""
    import ctypes

    if not hasattr(ctypes, "windll"):
        return lambda: None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    msg = wintypes.MSG()
    pm_remove = 0x0001

    def pump() -> None:
        try:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_remove):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass

    return pump


class BlockingDeviceWorker:
    """One long-lived thread that executes every blocking call for one device.

    ``initializer`` runs first, on the worker thread itself — the right place
    to load a vendor DLL so its handles belong to this thread and never cross
    threads.  ``await worker.call(fn, ...)`` posts a job and resolves with its
    result; exceptions propagate to the awaiting coroutine.
    """

    _PUMP_POLL_S = 0.005
    _PLAIN_POLL_S = 0.5

    def __init__(
        self,
        name: str,
        *,
        pump: bool = False,
        initializer: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self._pump_enabled = pump
        self._initializer = initializer
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._stopping = False
        self._init_error: BaseException | None = None
        self._init_done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    # -- async side ---------------------------------------------------------
    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn(*args, **kwargs)`` on the worker thread and await its result."""
        if self._stopping:
            raise RuntimeError(f"worker {self.name} is stopped")
        self._raise_if_init_failed()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._jobs.put((fn, args, kwargs, fut, loop))
        return await fut

    def call_sync(self, fn: Callable[..., T], *args: Any,
                  timeout: float = 60.0, **kwargs: Any) -> T:
        """Blocking variant for use outside the event loop (tests, shutdown)."""
        if self._stopping:
            raise RuntimeError(f"worker {self.name} is stopped")
        self._raise_if_init_failed()
        done = threading.Event()
        box: dict[str, Any] = {}

        def job() -> None:
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - relayed to caller
                box["error"] = exc
            finally:
                done.set()

        self._jobs.put((job, (), {}, None, None))
        if not done.wait(timeout):
            raise TimeoutError(f"worker {self.name}: call did not finish in {timeout}s")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def stop(self, join_timeout: float = 5.0) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._jobs.put(_STOP)
        self._thread.join(join_timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _raise_if_init_failed(self) -> None:
        # Surfaces a DLL-load failure on first use rather than swallowing it.
        if self._init_done.is_set() and self._init_error is not None:
            raise RuntimeError(
                f"worker {self.name} failed to initialize"
            ) from self._init_error

    # -- worker thread --------------------------------------------------------
    def _loop(self) -> None:
        try:
            if self._initializer is not None:
                self._initializer()
        except BaseException as exc:  # noqa: BLE001 - stored, re-raised on call()
            self._init_error = exc
        finally:
            self._init_done.set()

        pump = _make_win32_message_pump() if self._pump_enabled else None
        poll = self._PUMP_POLL_S if self._pump_enabled else self._PLAIN_POLL_S
        while True:
            try:
                job = self._jobs.get(timeout=poll)
            except queue.Empty:
                if pump is not None:
                    pump()
                continue
            if job is _STOP:
                return
            fn, args, kwargs, fut, loop = job
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - relayed to future
                self._relay(loop, fut, _set_exception_if_pending, exc)
            else:
                self._relay(loop, fut, _set_result_if_pending, result)
            if pump is not None:
                pump()

    def _relay(self, loop: asyncio.AbstractEventLoop | None,
               fut: asyncio.Future | None, setter: Callable[..., None],
               value: Any) -> None:
        """Post a result to the loop without ever killing the worker thread:
        a closed/stopped loop (shutdown race) is logged, not fatal — the
        transports keep a reference to this worker and would otherwise hold a
        dead thread forever (M-group)."""
        if fut is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(setter, fut, value)
        except RuntimeError as exc:
            logger.error("worker {}: cannot deliver result ({}); caller's loop "
                         "is gone", self.name, exc)


def _set_result_if_pending(fut: asyncio.Future, result: Any) -> None:
    if not fut.done():
        fut.set_result(result)


def _set_exception_if_pending(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
    else:
        # the awaiting coroutine was cancelled — don't lose the device error
        logger.opt(exception=exc).warning(
            "worker call finished with an exception after its caller was "
            "cancelled; error logged here so it is not silently dropped")


class WorkerPool:
    """Hands out exactly one worker per device (refactor.md §12.3 invariant)."""

    def __init__(self) -> None:
        self._workers: dict[str, BlockingDeviceWorker] = {}
        self._lock = threading.Lock()

    def for_device(
        self,
        instrument_id: str,
        *,
        pump: bool = False,
        initializer: Callable[[], None] | None = None,
    ) -> BlockingDeviceWorker:
        with self._lock:
            worker = self._workers.get(instrument_id)
            if worker is None or not worker.is_alive:
                worker = BlockingDeviceWorker(
                    f"dev-{instrument_id}", pump=pump, initializer=initializer
                )
                self._workers[instrument_id] = worker
            return worker

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()
