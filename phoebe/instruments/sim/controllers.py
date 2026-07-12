"""Sim controllers for every capability kind (refactor.md §14.2).

``backend = "sim"`` in the instrument config switches to these; the factory
injects a shared ``SimContext`` so the SLM's mask physically determines the
OSA's spectrum and the detector's voltage — the full loop closes offline.
"""
from __future__ import annotations

import asyncio

import numpy as np

from ...core.capability import InvocationContext
from ...core.config import InstrumentConfig
from ...core.contracts import timestamps
from ...core.controller import (
    DeviceHealth,
    DeviceIdentity,
    InstrumentController,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from ...core.factory import AppDependencies, ControllerFactoryRegistry
from ...domain.awg import OutputSetup, SequenceDefinition
from ...domain.daq import AnalogReadConfig, AnalogReadMeta, AnalogTrace
from ...domain.pattern import PatternSpec, SlmOptions, validate_frame
from ...domain.scope import (
    AcquisitionConfig,
    MonitorSample,
    MonitorSettings,
    ScopeWaveform,
    WaveformMeta,
)
from ...domain.spectrum import SpectrumTrace, TraceMeta, TraceRequest
from ..protocols import (
    KIND_ANALOG_INPUT,
    KIND_OSCILLOSCOPE,
    KIND_PATTERN_MODULATOR,
    KIND_SPECTRUM_ANALYZER,
    KIND_WAVEFORM_GENERATOR,
)
from .context import SimContext


class _SimControllerBase(InstrumentController):
    kind: str = ""

    def __init__(self, cfg: InstrumentConfig, sim: SimContext) -> None:
        super().__init__(cfg.instrument_id)
        self._cfg = cfg
        self._sim = sim
        self._connected = False

    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=self.kind,
            vendor=self._cfg.vendor, model=self._cfg.model,
            provides=(self.kind,),
        )

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            vendor=self._cfg.vendor, model=self._cfg.model,
            serial=f"SIM-{self.instrument_id}",
            raw=f"SIM,{self._cfg.vendor},{self._cfg.model},{self.instrument_id}",
        )

    async def get_health(self) -> DeviceHealth:
        return DeviceHealth(status="ok" if self._connected else "offline",
                            detail="simulated")

    async def get_snapshot(self) -> InstrumentSnapshot:
        return InstrumentSnapshot(
            instrument_id=self.instrument_id,
            values={"connected": self._connected, "backend": "sim"},
        )

    async def stop(self) -> None:
        return None

    async def safe_state(self) -> None:
        return None


class SimPatternModulator(_SimControllerBase):
    kind = KIND_PATTERN_MODULATOR

    def __init__(self, cfg: InstrumentConfig, sim: SimContext) -> None:
        super().__init__(cfg, sim)
        self._options = SlmOptions.model_validate(cfg.options)
        self._spec = self._options.spec()
        self._enabled = True

    def get_frame_spec(self) -> PatternSpec:
        return self._spec

    async def display_pattern(self, frame: np.ndarray, *,
                              context: InvocationContext) -> None:
        validate_frame(frame, self._spec)
        context.ensure_not_cancelled()
        async with self._op_lock:
            self._sim.current_mask = np.array(frame, copy=True)
            await asyncio.sleep(self._options.settle_ms / 1000)   # settle is simulated too

    async def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._sim.current_mask = None

    async def safe_state(self) -> None:
        self._sim.current_mask = None


class SimSpectrumAnalyzer(_SimControllerBase):
    kind = KIND_SPECTRUM_ANALYZER

    async def acquire_trace(self, request: TraceRequest, *,
                            context: InvocationContext) -> SpectrumTrace:
        scan = request.scan
        async with self._op_lock:
            context.ensure_not_cancelled()
            model = self._sim.model
            y = model.spectrum_dbm(self._sim.current_mask, scan)
            y = model.add_shot_noise(y, scan)
            await asyncio.sleep(model.sweep_time_s(scan))
            context.ensure_not_cancelled()
            return SpectrumTrace(
                x_nm=scan.wavelength_axis_nm(),
                y_dbm=np.clip(y, -120.0, 40.0),
                meta=TraceMeta(instrument_id=self.instrument_id, scan=scan,
                               trace_name=request.trace_name,
                               averages=scan.average_count, **timestamps()),
            )


class SimOscilloscope(_SimControllerBase):
    kind = KIND_OSCILLOSCOPE

    def __init__(self, cfg: InstrumentConfig, sim: SimContext) -> None:
        super().__init__(cfg, sim)
        self._acq: AcquisitionConfig | None = None
        self._rng = np.random.default_rng(99)

    async def configure(self, config: AcquisitionConfig, *,
                        context: InvocationContext) -> None:
        async with self._op_lock:
            self._acq = config

    async def acquire_waveform(self, channel: int, *,
                               context: InvocationContext) -> ScopeWaveform:
        async with self._op_lock:
            context.ensure_not_cancelled()
            time_range = self._acq.time_range_s if self._acq else 1e-3
            n = self._acq.record_length if self._acq and self._acq.record_length \
                else 1000
            level = self._sim.model.detector_volts(self._sim.current_mask)
            values = level + self._rng.normal(0, 1e-4, size=n)
            await asyncio.sleep(0.005)
            return ScopeWaveform(
                values=values.astype(np.float32),
                meta=WaveformMeta(instrument_id=self.instrument_id,
                                  channel=channel, x_start_s=0.0,
                                  x_stop_s=float(time_range), record_length=n,
                                  **timestamps()),
            )

    async def monitor_sample(self, settings: MonitorSettings, *,
                             context: InvocationContext) -> MonitorSample:
        async with self._op_lock:
            context.ensure_not_cancelled()
            if settings.hold_s:
                await asyncio.sleep(settings.hold_s)
            level = self._sim.model.detector_volts(self._sim.current_mask)
            noise = float(self._rng.normal(0, 1e-4))
            return MonitorSample(value=level + noise, std=1e-4, index=0,
                                 **timestamps())


class SimAnalogInput(_SimControllerBase):
    kind = KIND_ANALOG_INPUT

    def __init__(self, cfg: InstrumentConfig, sim: SimContext) -> None:
        super().__init__(cfg, sim)
        self._rng = np.random.default_rng(7)

    async def read_trace(self, config: AnalogReadConfig, *,
                         context: InvocationContext) -> AnalogTrace:
        async with self._op_lock:
            context.ensure_not_cancelled()
            if config.hold_s:
                await asyncio.sleep(config.hold_s)
            n = max(1, int(round(config.sample_rate_hz * config.duration_s)))
            level = self._sim.model.detector_volts(self._sim.current_mask)
            values = level + self._rng.normal(0, 1e-4, size=n)
            await asyncio.sleep(min(config.duration_s, 0.02))
            return AnalogTrace(
                values=values,
                meta=AnalogReadMeta(instrument_id=self.instrument_id,
                                    config=config, **timestamps()),
            )

    async def read_sample(self, config: AnalogReadConfig, *,
                          context: InvocationContext) -> MonitorSample:
        trace = await self.read_trace(config, context=context)
        return MonitorSample(value=trace.mean, std=trace.std, index=0,
                             **timestamps())


class SimWaveformGenerator(_SimControllerBase):
    kind = KIND_WAVEFORM_GENERATOR

    def __init__(self, cfg: InstrumentConfig, sim: SimContext) -> None:
        super().__init__(cfg, sim)
        self.deployed: SequenceDefinition | None = None
        self.setup: OutputSetup | None = None
        self.running = False
        self.trigger_count = 0

    async def deploy_sequence(self, sequence: SequenceDefinition, *,
                              context: InvocationContext) -> None:
        async with self._op_lock:
            context.ensure_not_cancelled()
            await asyncio.sleep(0.001 * len(sequence.waveforms))
            self.deployed = sequence

    async def configure_outputs(self, setup: OutputSetup, *,
                                context: InvocationContext) -> None:
        async with self._op_lock:
            self.setup = setup

    async def start_output(self, *, context: InvocationContext) -> None:
        async with self._op_lock:
            self.running = True

    async def stop_output(self, *, context: InvocationContext) -> None:
        async with self._op_lock:
            self.running = False

    async def force_trigger(self, *, context: InvocationContext) -> None:
        async with self._op_lock:
            self.trigger_count += 1

    async def stop(self) -> None:
        self.running = False

    async def safe_state(self) -> None:
        self.running = False


_SIM_CLASSES: dict[str, type[_SimControllerBase]] = {
    KIND_SPECTRUM_ANALYZER: SimSpectrumAnalyzer,
    KIND_PATTERN_MODULATOR: SimPatternModulator,
    KIND_OSCILLOSCOPE: SimOscilloscope,
    KIND_ANALOG_INPUT: SimAnalogInput,
    KIND_WAVEFORM_GENERATOR: SimWaveformGenerator,
}


def register_sim_factories(registry: ControllerFactoryRegistry) -> None:
    for kind, cls in _SIM_CLASSES.items():
        def factory(cfg: InstrumentConfig, deps: AppDependencies,
                    _cls: type[_SimControllerBase] = cls) -> InstrumentController:
            sim = deps.sim_context("tpa", SimContext)
            return _cls(cfg, sim)

        registry.register_sim(kind, factory)
