"""Santec SLM-200 controller (migrated from TPA_experiment slm_module/controller.py).

Carries the settled-semantics contract: ``display_pattern`` returns only after
the DLL call finished AND the configured liquid-crystal settle time elapsed
(refactor.md §4.4 contract 2).  The settle parameter lives in SlmOptions and
is recorded in every RunManifest; experiments never hand-write compensation
sleeps.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import numpy as np
from pydantic import Field, TypeAdapter

from ...core.capability import Capability, InvocationContext
from ...core.config import DllConnection, InstrumentConfig
from ...core.contracts import CapabilityId, ContractModel, InstrumentId
from ...core.controller import (
    DeviceHealth,
    DeviceIdentity,
    InstrumentController,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from ...core.errors import (
    InstrumentConnectionError,
    InstrumentError,
    UnsupportedInstrumentModelError,
)
from ...core.factory import AppDependencies
from ...core.worker import BlockingDeviceWorker
from ...domain.pattern import PatternSpec, SlmOptions, validate_frame
from ..protocols import KIND_PATTERN_MODULATOR
from .driver import MODE_DVI, SlmDllDriver


class GrayscaleRequest(ContractModel):
    level: Annotated[int, Field(ge=0, le=1023)]


class VerifyDviRequest(ContractModel):
    slm_number: Annotated[int, Field(ge=1)] = 1


SLM_DISPLAY_GRAYSCALE = Capability(
    id=CapabilityId("org.lab.pattern_modulator.display.v1.uniform-grayscale"),
    request_type=GrayscaleRequest,
    response_adapter=TypeAdapter(type(None)),
)

SLM_VERIFY_DVI = Capability(
    id=CapabilityId("org.lab.pattern_modulator.link.v1.verify-dvi"),
    request_type=VerifyDviRequest,
    response_adapter=TypeAdapter(bool),
)


class Slm200Options(SlmOptions):
    """SLM-200 factory options: base SlmOptions plus link management."""

    keepalive_interval_s: Annotated[float, Field(ge=0)] = 0.0   # 0 = disabled
    ensure_dvi_on_connect: bool = False
    usb_slm_number: Annotated[int, Field(ge=1)] = 1


class SantecSLM200Controller(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: SlmDllDriver,
                 worker: BlockingDeviceWorker, options: Slm200Options,
                 *, vendor: str = "santec", model: str = "slm-200") -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._worker = worker
        self._options = options
        self._spec = options.spec()
        self._vendor = vendor
        self._model = model
        self._connected = False
        self._enabled = True
        self._display_name = ""
        self._last_frame: np.ndarray | None = None
        self._last_kind: str | None = None          # "frame" | "grayscale"
        self._last_grayscale: int = 0
        self._keepalive_task: asyncio.Task | None = None
        self.capabilities.register(SLM_DISPLAY_GRAYSCALE, self._cap_grayscale,
                                   provider="santec.slm200")
        self.capabilities.register(SLM_VERIFY_DVI, self._cap_verify_dvi,
                                   provider="santec.slm200")

    # ------------------------------------------------------------- lifecycle
    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=KIND_PATTERN_MODULATOR,
            vendor=self._vendor, model=self._model,
            provides=(KIND_PATTERN_MODULATOR,),
        )

    async def connect(self) -> None:
        # Documented flow (Guide 1.3.2): search display → Disp_Open → display fns.
        width, height, name = await self._worker.call(self._driver.display_info)
        if not name.upper().startswith("LCOS-SLM"):
            raise InstrumentConnectionError(
                f"display {self._driver.display_no} reports {name!r}, "
                f"not an LCOS-SLM panel", instrument_id=str(self.instrument_id))
        if (height, width) != (self._spec.height, self._spec.width):
            raise InstrumentConnectionError(
                f"panel resolution {width}x{height} does not match configured "
                f"{self._spec.width}x{self._spec.height}",
                instrument_id=str(self.instrument_id))
        self._display_name = name
        if self._options.ensure_dvi_on_connect:
            await self._worker.call(self._driver.set_video_mode, MODE_DVI,
                                    self._options.usb_slm_number)
        await self._worker.call(self._driver.open_display)
        self._connected = True
        if self._options.keepalive_interval_s > 0:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name=f"slm-keepalive-{self.instrument_id}")

    async def disconnect(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._connected:
            self._connected = False
            await self._worker.call(self._driver.close_display)

    async def get_identity(self) -> DeviceIdentity:
        # The panel's EDID name ("LCOS-SLM...") is verified at connect; the
        # configured vendor/model tokens are echoed for the registry match.
        return DeviceIdentity(vendor=self._vendor, model=self._model,
                              raw=self._display_name or "not connected")

    async def get_health(self) -> DeviceHealth:
        if not self._connected:
            return DeviceHealth(status="offline")
        return DeviceHealth(status="ok", detail=self._display_name)

    async def get_snapshot(self) -> InstrumentSnapshot:
        values: dict[str, str | float | int | bool | None] = {
            "connected": self._connected,
            "enabled": self._enabled,
            "display_name": self._display_name,
            "settle_ms": self._options.settle_ms,
            "lut_id": self._options.lut_id,
            "last_display": self._last_kind,
        }
        if self._last_kind == "grayscale":
            values["last_grayscale"] = self._last_grayscale
        return InstrumentSnapshot(instrument_id=self.instrument_id, values=values)

    # ---------------------------------------------------- runtime contracts
    async def stop(self) -> None:
        """A DLL display call is short and cannot be aborted mid-flight; the
        worker queue simply drains.  Nothing to abort — but the method exists
        so the cleanup path is uniform."""

    async def safe_state(self) -> None:
        """Blank the panel (uniform 0 = no modulation)."""
        try:
            await self._worker.call(self._driver.load_grayscale, 0)
            self._last_kind = "grayscale"
            self._last_grayscale = 0
        except InstrumentError:
            pass

    async def unstage(self) -> None:
        # keepalive keeps refreshing whatever the run left on the panel
        return None

    # ------------------------------------------------- PatternModulator view
    def get_frame_spec(self) -> PatternSpec:
        return self._spec

    async def display_pattern(
        self, frame: np.ndarray, *, context: InvocationContext
    ) -> None:
        validate_frame(frame, self._spec)
        context.ensure_not_cancelled()
        async with self._op_lock:
            await self._worker.call(self._driver.display_frame, frame)
            self._last_frame = np.array(frame, copy=True)
            self._last_kind = "frame"
            await asyncio.sleep(self._options.settle_ms / 1000)   # settled, then return

    async def set_enabled(self, enabled: bool) -> None:
        async with self._op_lock:
            if enabled and self._last_kind == "frame" and self._last_frame is not None:
                await self._worker.call(self._driver.display_frame, self._last_frame)
                await asyncio.sleep(self._options.settle_ms / 1000)
            elif not enabled:
                await self._worker.call(self._driver.load_grayscale, 0)
            self._enabled = enabled

    def current_pattern(self) -> np.ndarray | None:
        """Copy of the exact grid last sent (for monitors); lock-free read."""
        if self._last_frame is None:
            return None
        return np.array(self._last_frame, copy=True)

    # ------------------------------------------------------------ capabilities
    async def _cap_grayscale(self, request: GrayscaleRequest,
                             context: InvocationContext) -> None:
        async with self._op_lock:
            context.ensure_not_cancelled()
            await self._worker.call(self._driver.load_grayscale, request.level)
            self._last_kind = "grayscale"
            self._last_grayscale = request.level
            self._last_frame = None
            await asyncio.sleep(self._options.settle_ms / 1000)

    async def _cap_verify_dvi(self, request: VerifyDviRequest,
                              context: InvocationContext) -> bool:
        mode = await self._worker.call(self._driver.get_video_mode,
                                       request.slm_number)
        return mode == MODE_DVI

    # ------------------------------------------------------------- keepalive
    async def _keepalive_loop(self) -> None:
        """Re-send the last pattern periodically so the DVI link stays warm
        (migrated from slm_module/keepalive.py).  Skips whenever an operation
        holds the lock — a run's own traffic is keepalive enough."""
        interval = self._options.keepalive_interval_s
        while True:
            await asyncio.sleep(interval)
            if self._op_lock.locked() or not self._connected:
                continue
            try:
                async with self._op_lock:
                    if self._last_kind == "frame" and self._last_frame is not None:
                        await self._worker.call(self._driver.display_frame,
                                                self._last_frame)
                    elif self._last_kind == "grayscale":
                        await self._worker.call(self._driver.load_grayscale,
                                                self._last_grayscale)
            except InstrumentError:
                continue


def build_slm200(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    options = Slm200Options.model_validate(cfg.options)
    match cfg.connection:
        case DllConnection() as c:
            driver = SlmDllDriver(c.dll_path, display_no=options.display_no,
                                  rate120=options.rate120)
        case _:
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: SLM-200 requires a vendor_dll connection")
    # pump=True: the DLL needs its Win32 message pump; initializer loads the
    # DLL on the worker thread so its handles never cross threads (§12.4).
    worker = deps.worker_pool.for_device(
        cfg.instrument_id, pump=True, initializer=driver.load)
    return SantecSLM200Controller(cfg.instrument_id, driver, worker, options,
                                  vendor=cfg.vendor, model=cfg.model)
