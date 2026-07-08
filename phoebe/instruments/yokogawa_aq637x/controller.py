"""AQ637X controller: lab semantics, atomic operations, runtime contracts
(migrated from TPA_experiment/src/osa_module/controller.py).

One acquisition = configure + trigger + poll + download under ONE operation
lock, so two tasks' SCPI can never interleave.  ``stop()`` sends :ABORt and
is the only path allowed to bypass the lock.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import numpy as np
from pydantic import Field, TypeAdapter

from ...core.capability import Capability, InvocationContext
from ...core.config import InstrumentConfig, TcpConnection
from ...core.contracts import CapabilityId, ContractModel, InstrumentId, Seconds, timestamps
from ...core.controller import (
    DeviceHealth,
    DeviceIdentity,
    InstrumentController,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from ...core.errors import (
    InstrumentError,
    InstrumentTimeoutError,
    InvalidInstrumentStateError,
    UnsupportedInstrumentModelError,
)
from ...core.factory import AppDependencies
from ...domain.spectrum import (
    Peak,
    PeakSearchRequest,
    SpectrumTrace,
    TraceMeta,
    TraceRequest,
    average_traces_dbm,
)
from ...transports.tcp import TcpScpiTransport
from ..protocols import KIND_SPECTRUM_ANALYZER
from .driver import AQ637XDriver, aq637x_handshake


class OsaOptions(ContractModel):
    """Second-stage validation of InstrumentConfig.options (refactor.md §5.2)."""

    username: str = "anonymous"
    password: str = ""
    command_format: Annotated[int, Field(ge=1, le=2)] = 1
    write_delay_s: Annotated[float, Field(ge=0)] = 0.1
    sweep_timeout_s: Seconds = 120.0
    poll_interval_s: Seconds = 0.5


OSA_FIND_PEAKS = Capability(
    id=CapabilityId("org.lab.spectrum_analyzer.peak-analysis.v1.find-peaks"),
    request_type=PeakSearchRequest,
    response_adapter=TypeAdapter(list[Peak]),
)


class AQ637XController(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: AQ637XDriver,
                 transport, options: OsaOptions, *,
                 vendor: str = "yokogawa", model: str = "aq6370") -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._transport = transport
        self._options = options
        self._vendor = vendor
        self._model = model
        self._identity: DeviceIdentity | None = None
        self._connected = False
        self._last_trace: SpectrumTrace | None = None
        self.capabilities.register(OSA_FIND_PEAKS, self._find_peaks,
                                   provider="yokogawa.aq637x")

    # ------------------------------------------------------------- lifecycle
    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=KIND_SPECTRUM_ANALYZER,
            vendor=self._vendor, model=self._model,
            provides=(KIND_SPECTRUM_ANALYZER,),
        )

    async def connect(self) -> None:
        await self._transport.open()
        await self._driver.initialize()
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        await self._transport.close()

    async def get_identity(self) -> DeviceIdentity:
        if self._identity is None:
            raw = await self._driver.identify()
            parts = [p.strip() for p in raw.split(",")]
            self._identity = DeviceIdentity(
                vendor=parts[0] if parts else "",
                model=parts[1] if len(parts) > 1 else "",
                serial=parts[2] if len(parts) > 2 else "",
                firmware=parts[3] if len(parts) > 3 else "",
                raw=raw,
            )
        return self._identity

    async def get_health(self) -> DeviceHealth:
        if not self._connected:
            return DeviceHealth(status="offline")
        try:
            await self._driver.identify()
            return DeviceHealth(status="ok")
        except InstrumentError as exc:
            return DeviceHealth(status="error", detail=str(exc))

    async def get_snapshot(self) -> InstrumentSnapshot:
        values: dict[str, str | float | int | bool | None] = {
            "connected": self._connected,
        }
        if self._connected:
            try:
                values["center_nm"] = await self._driver.query_center_nm()
                values["span_nm"] = await self._driver.query_span_nm()
                values["points"] = await self._driver.query_points()
            except InstrumentError as exc:
                values["snapshot_error"] = str(exc)
        return InstrumentSnapshot(instrument_id=self.instrument_id, values=values)

    async def stage(self) -> None:
        # single-sweep known state; stray sweeps from idle mode are aborted
        await self._driver.abort()

    # ---------------------------------------------------- runtime contracts
    async def stop(self) -> None:
        await self._driver.abort()               # deliberately no op_lock

    async def safe_state(self) -> None:
        await self._driver.abort()

    # ------------------------------------------------ SpectrumAnalyzer view
    async def acquire_trace(
        self, request: TraceRequest, *, context: InvocationContext
    ) -> SpectrumTrace:
        scan = request.scan
        async with self._op_lock:
            await self._driver.configure_scan(scan)
            await self._driver.select_trace(request.trace_name)
            spectra: list[np.ndarray] = []
            x_nm: np.ndarray | None = None
            for _ in range(scan.average_count):
                await self._sweep_once(context)
                if x_nm is None:
                    x_nm = await self._driver.read_trace_x_nm(request.trace_name)
                spectra.append(await self._driver.read_trace_y_dbm(request.trace_name))
            y_dbm = average_traces_dbm(spectra) if len(spectra) > 1 \
                else spectra[0].astype(np.float32)
            count = min(len(x_nm), len(y_dbm))    # guard against length mismatch
            trace = SpectrumTrace(
                x_nm=x_nm[:count],
                y_dbm=y_dbm[:count],
                meta=TraceMeta(
                    instrument_id=self.instrument_id, scan=scan,
                    trace_name=request.trace_name, averages=scan.average_count,
                    **timestamps(),
                ),
            )
            self._last_trace = trace
            return trace

    async def _sweep_once(self, context: InvocationContext) -> None:
        await self._driver.initiate_single_sweep()
        deadline = asyncio.get_running_loop().time() + self._options.sweep_timeout_s
        while not await self._driver.sweep_complete():
            context.ensure_not_cancelled()        # cancellation reaches the wait
            if asyncio.get_running_loop().time() >= deadline:
                raise InstrumentTimeoutError(
                    f"sweep did not complete within {self._options.sweep_timeout_s:.0f}s",
                    instrument_id=str(self.instrument_id),
                )
            await asyncio.sleep(self._options.poll_interval_s)

    # ------------------------------------------------------------ capability
    async def _find_peaks(
        self, request: PeakSearchRequest, context: InvocationContext
    ) -> list[Peak]:
        """Software peak search over the most recently acquired trace."""
        trace = self._last_trace
        if trace is None:
            raise InvalidInstrumentStateError(
                "no trace acquired yet; call acquire_trace first",
                instrument_id=str(self.instrument_id),
            )
        y = trace.y_dbm
        peaks: list[Peak] = []
        for i in range(1, len(y) - 1):
            if y[i] >= request.threshold_dbm and y[i] >= y[i - 1] and y[i] > y[i + 1]:
                peaks.append(Peak(wavelength_nm=float(trace.x_nm[i]),
                                  power_dbm=float(np.clip(y[i], -120.0, 40.0))))
        peaks.sort(key=lambda p: p.power_dbm, reverse=True)
        return peaks[: request.max_peaks]


def build_aq637x(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    options = OsaOptions.model_validate(cfg.options)
    match cfg.connection:
        case TcpConnection() as c:
            transport = TcpScpiTransport(
                c.host, c.port,
                worker=deps.worker_pool.for_device(cfg.instrument_id),
                timeout_s=c.timeout_s,
                write_delay_s=options.write_delay_s,
                on_open=aq637x_handshake(options.username, options.password),
            )
        case _:
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: AQ637X requires a tcp connection")
    driver = AQ637XDriver(transport, command_format=options.command_format)
    return AQ637XController(cfg.instrument_id, driver, transport, options,
                            vendor=cfg.vendor, model=cfg.model)
