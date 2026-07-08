"""AWG5204 controller: lab semantics, atomic operations, runtime contracts
(migrated from awg5204/awg5204_tm — driver.py orchestration plus
hardware.py/sequence.py/waveform.py semantics).

Implements the :class:`WaveformGenerator` capability.  A deployment
(delete-old -> upload waveforms -> program the playlist -> assign to channels)
runs as ONE atomic operation under ``self._op_lock`` so two tasks' SCPI can
never interleave.  ``stop()`` sends ``AWGControl:STOP:IMMediate`` and is the
only path allowed to bypass the lock.

The underlying driver speaks raw SCPI over an injected transport; the original
tm_devices command-tree dependency has been removed.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...core.capability import InvocationContext
from ...core.config import InstrumentConfig, TcpConnection, VisaConnection
from ...core.contracts import ContractModel, InstrumentId
from ...core.controller import (
    DeviceHealth,
    DeviceIdentity,
    InstrumentController,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from ...core.errors import InstrumentError, UnsupportedInstrumentModelError
from ...core.factory import AppDependencies
from ...domain.awg import OutputSetup, SequenceDefinition, normalise_waveforms
from ...transports.visa import VisaScpiTransport
from ..protocols import KIND_WAVEFORM_GENERATOR
from .driver import AWG5204Driver


class AwgOptions(ContractModel):
    """Second-stage validation of InstrumentConfig.options (refactor.md §5.2)."""

    channels: Annotated[int, Field(ge=1, le=4)] = 4


class AWG5204Controller(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: AWG5204Driver,
                 transport, options: AwgOptions, *,
                 vendor: str = "tektronix", model: str = "awg5204") -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._transport = transport
        self._options = options
        self._vendor = vendor
        self._model = model
        self._identity: DeviceIdentity | None = None
        self._connected = False

    # ------------------------------------------------------------- lifecycle
    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=KIND_WAVEFORM_GENERATOR,
            vendor=self._vendor, model=self._model,
            provides=(KIND_WAVEFORM_GENERATOR,),
        )

    async def connect(self) -> None:
        await self._transport.open()          # no *RST on connect (keep device state)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        await self._transport.close()

    async def get_identity(self) -> DeviceIdentity:
        if self._identity is None:
            raw = await self._driver.identify()        # "TEKTRONIX,AWG5204,serial,fw"
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
                values["run_state"] = await self._driver.query_run_state()
            except InstrumentError as exc:
                values["snapshot_error"] = str(exc)
        return InstrumentSnapshot(instrument_id=self.instrument_id, values=values)

    async def stage(self) -> None:
        # Known state before a run: clear the status/event registers.
        await self._driver.clear_status()

    # ---------------------------------------------------- runtime contracts
    async def stop(self) -> None:
        await self._driver.stop()                # deliberately no op_lock

    async def safe_state(self) -> None:
        await self._driver.stop()
        for ch in range(1, self._options.channels + 1):
            try:
                await self._driver.set_output_state(ch, False)
            except InstrumentError:
                pass

    # ------------------------------------------- WaveformGenerator view
    async def deploy_sequence(
        self, sequence: SequenceDefinition, *, context: InvocationContext
    ) -> None:
        context.ensure_not_cancelled()
        normalised = normalise_waveforms(tuple(sequence.waveforms.values()))
        waveforms = {wf.name: wf for wf in normalised}
        async with self._op_lock:
            # Best-effort cleanup: deleting an absent item errors on the device.
            try:
                await self._driver.delete_sequence(sequence.name)
            except InstrumentError:
                pass
            for name in waveforms:
                try:
                    await self._driver.delete_waveform(name)
                except InstrumentError:
                    pass

            # Upload every waveform (samples + markers when present).
            for wf in waveforms.values():
                context.ensure_not_cancelled()
                await self._driver.new_waveform(wf.name, wf.length)
                await self._driver.upload_waveform_samples(wf.name, wf.samples)
                bits = wf.combined_marker_bits()
                if bits.any():
                    await self._driver.upload_marker_data(wf.name, bits)

            # Program the playlist.
            context.ensure_not_cancelled()
            await self._driver.new_sequence(sequence.name, len(sequence.steps))
            for index, step in enumerate(sequence.steps, start=1):
                for track, waveform_name in sorted(step.track_waveforms.items()):
                    await self._driver.assign_step_waveform(
                        sequence.name, index, track, waveform_name)
                await self._driver.set_step_repeat(
                    sequence.name, index, step.repeat_token)

            # Match instrument timing to the synthesized waveforms.
            await self._driver.set_clock_sample_rate(sequence.sample_rate)
            await self._driver.set_sequence_sample_rate(
                sequence.name, sequence.sample_rate)

            # Assign the sequence to one channel per track.
            context.ensure_not_cancelled()
            for ch in range(1, sequence.tracks + 1):
                await self._driver.assign_sequence_to_channel(
                    ch, sequence.name, ch)

    async def configure_outputs(
        self, setup: OutputSetup, *, context: InvocationContext
    ) -> None:
        context.ensure_not_cancelled()
        async with self._op_lock:
            for channel in setup.channels:
                await self._driver.set_output_state(channel.channel, channel.enabled)
                await self._driver.set_amplitude(channel.channel, channel.amplitude_vpp)
                await self._driver.set_offset(channel.channel, channel.offset_v)
            for marker in setup.markers:
                await self._driver.configure_marker(marker)
            await self._driver.set_run_mode(setup.run_mode)
            await self._driver.configure_trigger(setup.trigger)

    async def start_output(self, *, context: InvocationContext) -> None:
        context.ensure_not_cancelled()
        async with self._op_lock:
            await self._driver.run()

    async def stop_output(self, *, context: InvocationContext) -> None:
        async with self._op_lock:
            await self._driver.stop()

    async def force_trigger(self, *, context: InvocationContext) -> None:
        context.ensure_not_cancelled()
        async with self._op_lock:
            await self._driver.force_trigger()


def build_awg5204(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    options = AwgOptions.model_validate(cfg.options)
    worker = deps.worker_pool.for_device(cfg.instrument_id)
    match cfg.connection:
        case VisaConnection() as c:
            transport = VisaScpiTransport(
                c.resource_name, worker=worker,
                timeout_s=c.timeout_s, visa_library=c.visa_library,
            )
        case TcpConnection() as c:
            # The AWG accepts a raw-socket VISA resource for TCP connections.
            transport = VisaScpiTransport(
                f"TCPIP::{c.host}::{c.port}::SOCKET", worker=worker,
                timeout_s=c.timeout_s,
            )
        case _:
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: AWG5204 requires a visa or tcp connection")
    driver = AWG5204Driver(transport)
    return AWG5204Controller(cfg.instrument_id, driver, transport, options,
                             vendor=cfg.vendor, model=cfg.model)
