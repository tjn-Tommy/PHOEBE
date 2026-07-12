"""Device lifecycle FSM + reconnect supervision (plan §6.3, PRs B-1/B-2).

One ``DeviceSupervisor`` per configured instrument owns that device's
lifecycle state and every reconnect decision::

    CONFIGURED → CONNECTING → READY            (identity verified)
    CONNECTING → BACKOFF → CONNECTING          (transient failure, policy timer)
    CONNECTING → ERROR                         (fatal: bad address, missing DLL,
                                                identity mismatch — no blind retry)
    BACKOFF → OFFLINE                          (give-up ceiling)
    READY → DEGRADED → READY                   (probe failed / probe ok)
    DEGRADED → BACKOFF                         (failure threshold → handle rebuild)
    READY → OFFLINE / OFFLINE|ERROR → CONNECTING  (operator disable / reconnect)

The supervisor never raises out of a connect attempt — a failed device boots
the app DEGRADED instead of aborting it (plan: "startup is degraded-tolerant").
Lease acquisition is legal only in READY; ``DeviceManager`` enforces that.

Rebuilds run through the controller's ``disconnect()``/``connect()``, whose
blocking work already lives on the device's worker thread (DLL affinity), and
are lease-aware: a device with an active lease is never rebuilt underneath
its run (plan §3.2 B2).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal

from loguru import logger

from .config import ReconnectSettings
from .contracts import InstrumentId
from .retry import ErrorClass, RetryPolicy, classify_error


class DeviceLifecycleState(StrEnum):
    CONFIGURED = "configured"    # built, never connected (supervision not engaged)
    CONNECTING = "connecting"
    READY = "ready"              # connected + identity verified; leasable
    DEGRADED = "degraded"        # probe failed; still connected, not leasable
    BACKOFF = "backoff"          # waiting for the retry timer
    OFFLINE = "offline"          # gave up / operator disabled; manual reconnect
    ERROR = "error"              # fatal — operator must fix config/hardware first


HealthStatus = Literal["ok", "degraded", "error", "offline"]

#: Health-event projection of the lifecycle state (DeviceHealthEvent.status).
HEALTH_STATUS_OF: dict[DeviceLifecycleState, HealthStatus] = {
    DeviceLifecycleState.CONFIGURED: "offline",
    DeviceLifecycleState.CONNECTING: "degraded",
    DeviceLifecycleState.READY: "ok",
    DeviceLifecycleState.DEGRADED: "degraded",
    DeviceLifecycleState.BACKOFF: "degraded",
    DeviceLifecycleState.OFFLINE: "offline",
    DeviceLifecycleState.ERROR: "error",
}

_LEGAL: dict[DeviceLifecycleState, frozenset[DeviceLifecycleState]] = {
    DeviceLifecycleState.CONFIGURED: frozenset({DeviceLifecycleState.CONNECTING,
                                                DeviceLifecycleState.OFFLINE}),
    DeviceLifecycleState.CONNECTING: frozenset({DeviceLifecycleState.READY,
                                                DeviceLifecycleState.BACKOFF,
                                                DeviceLifecycleState.ERROR,
                                                DeviceLifecycleState.OFFLINE}),
    DeviceLifecycleState.READY: frozenset({DeviceLifecycleState.DEGRADED,
                                           DeviceLifecycleState.OFFLINE,
                                           DeviceLifecycleState.CONNECTING}),
    DeviceLifecycleState.DEGRADED: frozenset({DeviceLifecycleState.READY,
                                              DeviceLifecycleState.BACKOFF,
                                              DeviceLifecycleState.OFFLINE,
                                              DeviceLifecycleState.CONNECTING}),
    DeviceLifecycleState.BACKOFF: frozenset({DeviceLifecycleState.CONNECTING,
                                             DeviceLifecycleState.OFFLINE}),
    DeviceLifecycleState.OFFLINE: frozenset({DeviceLifecycleState.CONNECTING}),
    DeviceLifecycleState.ERROR: frozenset({DeviceLifecycleState.CONNECTING,
                                           DeviceLifecycleState.OFFLINE}),
}

StateListener = Callable[[InstrumentId, DeviceLifecycleState, str | None], None]


class DeviceSupervisor:
    """Owns one device's lifecycle state and reconnect loop.

    ``connect`` / ``disconnect`` are injected (DeviceManager's
    ``connect_instrument`` — connect + identity verification — and the
    controller's ``disconnect``), so the supervisor stays free of device
    detail.  All methods run on the core loop.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        *,
        connect: Callable[[], Awaitable[None]],
        disconnect: Callable[[], Awaitable[None]],
        settings: ReconnectSettings,
        on_state_change: StateListener | None = None,
    ) -> None:
        self.instrument_id = instrument_id
        self._connect = connect
        self._disconnect = disconnect
        self._settings = settings
        self._backoff = RetryPolicy(
            max_attempts=max(settings.give_up_attempts, 1),
            base_delay_s=settings.base_delay_s,
            max_delay_s=settings.max_delay_s,
            multiplier=settings.multiplier,
        )
        self._on_state_change = on_state_change
        self.state = DeviceLifecycleState.CONFIGURED
        self.last_error: BaseException | None = None
        self._connect_failures = 0
        self._probe_failures = 0
        self._last_confirm_mono: float | None = None   # READY entry / probe ok
        self._retry_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()   # one connect attempt at a time

    # ------------------------------------------------------------- queries
    @property
    def is_ready(self) -> bool:
        return self.state is DeviceLifecycleState.READY

    @property
    def detail(self) -> str | None:
        return str(self.last_error) if self.last_error is not None else None

    def health_age_s(self) -> float | None:
        """Seconds since the device last *proved* healthy (READY entry or a
        successful probe); None before the first confirmation.  Admission's
        HEALTH_STALE gate reads this — cached state only, no device I/O."""
        if self._last_confirm_mono is None:
            return None
        return time.monotonic() - self._last_confirm_mono

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> bool:
        """First connect attempt; failures arm the FSM instead of raising."""
        return await self._try_connect()

    async def stop(self) -> None:
        """Cancel any pending retry (app shutdown); leaves the state as-is."""
        self._cancel_retry()

    async def disable(self) -> None:
        """Operator disable: no automatic reconnection until reconnect_now()."""
        self._cancel_retry()
        try:
            await self._disconnect()
        except Exception as exc:
            logger.warning("{}: disconnect during disable failed: {}",
                           self.instrument_id, exc)
        self._set_state(DeviceLifecycleState.OFFLINE, "operator disable")

    async def reconnect_now(self) -> bool:
        """Operator reconnect (also legal from ERROR after a config fix)."""
        self._cancel_retry()
        self._connect_failures = 0
        try:
            await self._disconnect()
        except Exception:
            pass                            # dead handle — that's why we're here
        return await self._try_connect()

    # ---------------------------------------------------------- probe input
    async def note_probe_ok(self) -> None:
        self._probe_failures = 0
        self._last_confirm_mono = time.monotonic()
        if self.state is DeviceLifecycleState.DEGRADED:
            self._set_state(DeviceLifecycleState.READY, "probe recovered")

    async def note_probe_failure(self, exc: BaseException, *,
                                 allow_rebuild: bool = True) -> None:
        """Health probe failed.  READY → DEGRADED; repeated failures trigger a
        handle rebuild — unless the device is leased (never rebuild under an
        active run; plan §3.2 B2) or the failure is fatal."""
        self.last_error = exc
        if self.state is DeviceLifecycleState.READY:
            self._probe_failures = 1
            self._set_state(DeviceLifecycleState.DEGRADED, str(exc))
            return
        if self.state is not DeviceLifecycleState.DEGRADED:
            return
        self._probe_failures += 1
        if classify_error(exc) is ErrorClass.FATAL:
            self._cancel_retry()
            self._set_state(DeviceLifecycleState.ERROR, str(exc))
            return
        if (allow_rebuild
                and self._probe_failures >= self._settings.rebuild_after_probe_failures):
            logger.warning("{}: {} consecutive probe failures — rebuilding handle",
                           self.instrument_id, self._probe_failures)
            self._set_state(DeviceLifecycleState.BACKOFF, "rebuild triggered")
            try:
                await self._disconnect()
            except Exception:
                pass                        # handle already dead
            self._probe_failures = 0
            await self._try_connect()

    # ------------------------------------------------------------ internals
    async def _try_connect(self) -> bool:
        async with self._connect_lock:
            if self.state is DeviceLifecycleState.READY:
                return True
            self._set_state(DeviceLifecycleState.CONNECTING)
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.last_error = exc
                return self._on_connect_failure(exc)
            self.last_error = None
            self._connect_failures = 0
            self._probe_failures = 0
            self._last_confirm_mono = time.monotonic()
            self._set_state(DeviceLifecycleState.READY, "connected")
            return True

    def _on_connect_failure(self, exc: BaseException) -> bool:
        if classify_error(exc) is ErrorClass.FATAL:
            logger.error("{}: fatal connect failure — not retrying: {}",
                         self.instrument_id, exc)
            self._set_state(DeviceLifecycleState.ERROR, str(exc))
            return False
        self._connect_failures += 1
        ceiling = self._settings.give_up_attempts
        if ceiling and self._connect_failures >= ceiling:
            logger.error("{}: giving up after {} connect attempts: {}",
                         self.instrument_id, self._connect_failures, exc)
            self._set_state(DeviceLifecycleState.OFFLINE,
                            f"gave up after {self._connect_failures} attempts: {exc}")
            return False
        delay = self._backoff.delay_for(self._connect_failures)
        logger.warning("{}: connect failed ({}); retry #{} in {:.2f}s",
                       self.instrument_id, exc, self._connect_failures, delay)
        self._set_state(DeviceLifecycleState.BACKOFF, str(exc))
        self._arm_retry(delay)
        return False

    def _arm_retry(self, delay: float) -> None:
        self._cancel_retry()
        self._retry_task = asyncio.create_task(
            self._retry_after(delay), name=f"reconnect-{self.instrument_id}")

    async def _retry_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._try_connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("{}: reconnect attempt crashed", self.instrument_id)

    def _cancel_retry(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None

    def _set_state(self, new: DeviceLifecycleState, detail: str | None = None) -> None:
        if new is self.state:
            return
        if new not in _LEGAL[self.state]:
            # Log, don't raise: an FSM bug must degrade observability, never
            # brick device management on a live bench.
            logger.error("{}: illegal lifecycle transition {} → {} (forced)",
                         self.instrument_id, self.state, new)
        logger.info("{}: lifecycle {} → {}{}", self.instrument_id, self.state, new,
                    f" ({detail})" if detail else "")
        self.state = new
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.instrument_id, new, detail)
            except Exception:
                logger.exception("{}: state-change listener failed", self.instrument_id)
