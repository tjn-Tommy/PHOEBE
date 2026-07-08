"""Base capability protocols — the stable surface experiments depend on
(refactor.md §4.4).

Capability views deliberately exclude ``connect/disconnect``: an experiment
receives an already-connected, already-leased instance; lifecycle belongs to
the DeviceManager.  Capability matching is declarative — each protocol is
registered against its kind string here; nobody isinstance-sniffs Protocols.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..core.capability import InvocationContext
from ..core.di import register_capability_kind
from ..domain.daq import AnalogReadConfig, AnalogTrace
from ..domain.pattern import PatternSpec
from ..domain.scope import (
    AcquisitionConfig,
    MonitorSample,
    MonitorSettings,
    ScopeWaveform,
)
from ..domain.spectrum import SpectrumTrace, TraceRequest
from ..domain.awg import OutputSetup, SequenceDefinition

KIND_SPECTRUM_ANALYZER = "spectrum_analyzer"
KIND_PATTERN_MODULATOR = "pattern_modulator"
KIND_OSCILLOSCOPE = "oscilloscope"
KIND_ANALOG_INPUT = "analog_input"
KIND_WAVEFORM_GENERATOR = "waveform_generator"


@runtime_checkable
class SpectrumAnalyzer(Protocol):
    async def acquire_trace(
        self, request: TraceRequest, *, context: InvocationContext
    ) -> SpectrumTrace: ...


@runtime_checkable
class PatternModulator(Protocol):
    def get_frame_spec(self) -> PatternSpec: ...

    async def display_pattern(
        self, frame: np.ndarray, *, context: InvocationContext
    ) -> None:
        """Returns only once the pattern is physically settled (§4.4 contract 2)."""
        ...

    async def set_enabled(self, enabled: bool) -> None: ...


@runtime_checkable
class Oscilloscope(Protocol):
    async def configure(
        self, config: AcquisitionConfig, *, context: InvocationContext
    ) -> None: ...

    async def acquire_waveform(
        self, channel: int, *, context: InvocationContext
    ) -> ScopeWaveform: ...

    async def monitor_sample(
        self, settings: MonitorSettings, *, context: InvocationContext
    ) -> MonitorSample: ...


@runtime_checkable
class AnalogInput(Protocol):
    async def read_trace(
        self, config: AnalogReadConfig, *, context: InvocationContext
    ) -> AnalogTrace: ...

    async def read_sample(
        self, config: AnalogReadConfig, *, context: InvocationContext
    ) -> MonitorSample: ...


@runtime_checkable
class WaveformGenerator(Protocol):
    async def deploy_sequence(
        self, sequence: SequenceDefinition, *, context: InvocationContext
    ) -> None: ...

    async def configure_outputs(
        self, setup: OutputSetup, *, context: InvocationContext
    ) -> None: ...

    async def start_output(self, *, context: InvocationContext) -> None: ...

    async def stop_output(self, *, context: InvocationContext) -> None: ...

    async def force_trigger(self, *, context: InvocationContext) -> None: ...


# Declarative protocol → kind table used by the DI resolver (§7.2).
register_capability_kind(SpectrumAnalyzer, KIND_SPECTRUM_ANALYZER)
register_capability_kind(PatternModulator, KIND_PATTERN_MODULATOR)
register_capability_kind(Oscilloscope, KIND_OSCILLOSCOPE)
register_capability_kind(AnalogInput, KIND_ANALOG_INPUT)
register_capability_kind(WaveformGenerator, KIND_WAVEFORM_GENERATOR)
