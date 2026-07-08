"""RTO6 controller: lab semantics, atomic operations, runtime contracts.

Migrated from ``TPA_experiment/src/scope_module/controller.py``.

One acquisition = arm + poll + download under ONE operation lock, so two
tasks' SCPI can never interleave.  ``stop()`` writes STOP and is the only path
allowed to bypass the lock.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from ...core.capability import InvocationContext
from ...core.config import InstrumentConfig, TcpConnection, VisaConnection
from ...core.contracts import ContractModel, InstrumentId, timestamps
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
    UnsupportedInstrumentModelError,
)
from ...core.factory import AppDependencies
from ...domain.scope import (
    AcquisitionConfig,
    MonitorSample,
    MonitorSettings,
    ScopeWaveform,
    WaveformMeta,
)
from ...transports.visa import VisaScpiTransport
from ..protocols import KIND_OSCILLOSCOPE
from .driver import RTO6Driver


class ScopeOptions(ContractModel):
    """Second-stage validation of InstrumentConfig.options (refactor.md §5.2)."""

    acquisition_timeout_s: Annotated[float, Field(gt=0)] = 60.0
    poll_interval_s: Annotated[float, Field(gt=0)] = 0.2
    mean_group: Annotated[int, Field(ge=1)] = 1
    stddev_group: Annotated[int, Field(ge=1)] = 2


class RTO6Controller(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: RTO6Driver,
                 transport, options: ScopeOptions, *,
                 vendor: str = "rohde&schwarz", model: str = "rto6") -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._transport = transport
        self._options = options
        self._vendor = vendor
        self._model = model
        self._identity: DeviceIdentity | None = None
        self._connected = False
        self._config: AcquisitionConfig | None = None   # for snapshots & monitor gates

    # ------------------------------------------------------------- lifecycle
    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=KIND_OSCILLOSCOPE,
            vendor=self._vendor, model=self._model,
            provides=(KIND_OSCILLOSCOPE,),
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
            # *IDN? -> "ROHDE&SCHWARZ,RTO6,<serial>,<firmware>"
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
                values["sample_rate"] = await self._driver.sample_rate()
                values["record_length"] = await self._driver.record_length()
            except InstrumentError as exc:
                values["snapshot_error"] = str(exc)
        return InstrumentSnapshot(instrument_id=self.instrument_id, values=values)

    # ---------------------------------------------------- runtime contracts
    async def stop(self) -> None:
        await self._driver.stop()                # deliberately no op_lock

    async def safe_state(self) -> None:
        await self._driver.stop()

    # ------------------------------------------------------ Oscilloscope view
    async def configure(
        self, config: AcquisitionConfig, *, context: InvocationContext
    ) -> None:
        async with self._op_lock:
            for ch in config.channels:
                await self._driver.configure_channel(
                    ch.channel,
                    state=ch.enabled,
                    scale=ch.scale_v_per_div,
                    offset=ch.offset_v,
                    coupling=ch.coupling,
                )
                if config.decimation is not None:
                    await self._driver.set_decimation(ch.channel, config.decimation)
                if config.arithmetics is not None:
                    await self._driver.set_arithmetics(ch.channel, config.arithmetics)
                if ch.bandwidth is not None:
                    await self._driver.set_bandwidth_limit(ch.channel, ch.bandwidth)
                if ch.digital_filter_cutoff_hz is not None:
                    await self._driver.set_digital_filter(
                        ch.channel, ch.digital_filter_cutoff_hz)
            trig = config.trigger
            await self._driver.set_trigger(
                source=trig.source, level=trig.level_v,
                slope=trig.slope, mode=trig.mode,
            )
            await self._driver.set_time_range(config.time_range_s)
            await self._driver.set_acquisition_count(config.acquisition_count)
            await self._driver.set_record_length(config.record_length)
            if config.post_trigger_window:
                await self._driver.set_post_trigger_window()
            self._config = config

    async def acquire_waveform(
        self, channel: int, *, context: InvocationContext
    ) -> ScopeWaveform:
        async with self._op_lock:
            await self._driver.single_acquisition()
            await self._poll_until_complete(context)
            x_start, x_stop, record_length, values_per_sample = (
                await self._driver.read_waveform_header(channel)
            )
            values = await self._driver.read_waveform(channel)
            return ScopeWaveform(
                values=values,
                meta=WaveformMeta(
                    instrument_id=self.instrument_id,
                    channel=channel,
                    x_start_s=x_start,
                    x_stop_s=x_stop,
                    record_length=record_length,
                    values_per_sample=values_per_sample,
                    **timestamps(),
                ),
            )

    async def monitor_sample(
        self, settings: MonitorSettings, *, context: InvocationContext
    ) -> MonitorSample:
        async with self._op_lock:
            if settings.hold_s:
                await asyncio.sleep(settings.hold_s)   # settle after pattern change
            ch = settings.channel
            # group 1 = gated MEAN, group 2 = gated STDDev over the same window
            await self._driver.setup_mean_measurement(
                ch, group=self._options.mean_group,
                gate_start=settings.gate_start_s, gate_stop=settings.gate_stop_s,
            )
            await self._driver.setup_stddev_measurement(
                ch, group=self._options.stddev_group,
                gate_start=settings.gate_start_s, gate_stop=settings.gate_stop_s,
            )
            await self._driver.single_acquisition()
            await self._poll_until_complete(context)
            value = await self._driver.read_measurement(self._options.mean_group)
            std = await self._driver.read_measurement(self._options.stddev_group)
            return MonitorSample(value=value, std=std, index=0, **timestamps())

    # --------------------------------------------------------------- helpers
    async def _poll_until_complete(self, context: InvocationContext) -> None:
        """Poll the OPC bit until the acquisition finishes or we time out.

        Each poll is a separate query, so the wait never blocks the transport
        between polls, and cancellation reaches into the wait.
        """
        deadline = (asyncio.get_running_loop().time()
                    + self._options.acquisition_timeout_s)
        while not await self._driver.is_acquisition_complete():
            context.ensure_not_cancelled()          # cancellation reaches the wait
            if asyncio.get_running_loop().time() >= deadline:
                raise InstrumentTimeoutError(
                    f"acquisition did not complete within "
                    f"{self._options.acquisition_timeout_s:.0f}s",
                    instrument_id=str(self.instrument_id),
                )
            await asyncio.sleep(self._options.poll_interval_s)


def build_rto6(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    options = ScopeOptions.model_validate(cfg.options)
    match cfg.connection:
        case VisaConnection() as c:
            transport = VisaScpiTransport(
                c.resource_name,
                worker=deps.worker_pool.for_device(cfg.instrument_id),
                timeout_s=c.timeout_s,
                visa_library=c.visa_library,
            )
        case TcpConnection():
            # HiSLIP (TCPIP::<host>::hislip0::INSTR) is R&S's high-throughput LAN
            # protocol, but it needs a VISA library -- the raw-TCP SCPI transport
            # cannot speak it.  Use a visa connection instead.
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: RTO6 requires a visa connection "
                f"(HiSLIP over a raw tcp transport is not supported); configure a "
                f"visa resource such as 'TCPIP::<host>::hislip0::INSTR'"
            )
        case _:
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: RTO6 requires a visa connection")
    driver = RTO6Driver(transport)
    return RTO6Controller(cfg.instrument_id, driver, transport, options,
                          vendor=cfg.vendor, model=cfg.model)
