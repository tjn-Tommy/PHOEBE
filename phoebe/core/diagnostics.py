"""Event-loop diagnostics: lag monitor + wedged-loop watchdog (plan §3.1 A5).

PHOEBE's topology — one asyncio loop thread owning all of core, ringed by
per-device blocking worker threads — makes a wedged loop (a blocking call
that leaked onto the loop) both catastrophic and invisible: the UI just stops
updating and nothing crashes.  Two instruments here:

* **Lag monitor** (on the loop): measures scheduling delay of a periodic
  sleep; sustained delay means something is hogging the loop.  Logged, and
  the maximum is kept for health reporting.
* **Watchdog** (its OWN daemon thread, so it survives the wedge): watches the
  heartbeat the lag monitor refreshes; when the heartbeat stalls past the
  threshold it dumps *every* thread's stack via ``faulthandler`` — the
  post-mortem shows exactly which call wedged the loop.
"""
from __future__ import annotations

import asyncio
import faulthandler
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger


class EventLoopDiagnostics:
    """Attach to a running loop from any thread; ``stop()`` detaches cleanly."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        heartbeat_interval_s: float = 0.25,
        lag_warn_s: float = 0.25,
        stall_dump_s: float = 5.0,
        dump_path: Path | None = None,
        on_stall: Callable[[float], None] | None = None,
    ) -> None:
        self._loop = loop
        self._interval = heartbeat_interval_s
        self._lag_warn_s = lag_warn_s
        self._stall_dump_s = stall_dump_s
        self._dump_path = dump_path
        self._on_stall = on_stall
        self.max_lag_s = 0.0
        self.stall_count = 0
        self._last_beat = time.monotonic()
        self._stalled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._beat_future = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._last_beat = time.monotonic()
        self._beat_future = asyncio.run_coroutine_threadsafe(
            self._beat_loop(), self._loop)
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._beat_future is not None:
            self._beat_future.cancel()
            self._beat_future = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------ loop-side monitor
    async def _beat_loop(self) -> None:
        while True:
            start = time.monotonic()
            await asyncio.sleep(self._interval)
            now = time.monotonic()
            lag = now - start - self._interval
            if lag > self.max_lag_s:
                self.max_lag_s = lag
            if lag > self._lag_warn_s:
                logger.warning(
                    "event loop lag {:.3f}s — something is blocking the core "
                    "loop (blocking calls belong on device workers)", lag)
            self._last_beat = now

    # ----------------------------------------------------- watchdog thread
    def _watch(self) -> None:
        poll = max(0.05, min(self._interval, self._stall_dump_s) / 2)
        while not self._stop_event.wait(poll):
            silence = time.monotonic() - self._last_beat
            if silence >= self._stall_dump_s:
                if not self._stalled:            # one dump per stall episode
                    self._stalled = True
                    self.stall_count += 1
                    self._dump(silence)
            else:
                self._stalled = False

    def _dump(self, silence: float) -> None:
        logger.error("event loop wedged for {:.1f}s — dumping all thread stacks",
                     silence)
        try:
            if self._dump_path is not None:
                with open(self._dump_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== event loop stall: silent {silence:.1f}s, "
                             f"t={time.time():.0f} ===\n")
                    faulthandler.dump_traceback(file=fh, all_threads=True)
            else:
                faulthandler.dump_traceback(all_threads=True)   # stderr
        except Exception:
            logger.exception("thread-stack dump failed")
        if self._on_stall is not None:
            try:
                self._on_stall(silence)
            except Exception:
                logger.exception("on_stall callback failed")
