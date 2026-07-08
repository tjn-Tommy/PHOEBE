"""AWG domain models (migrated from awg5204/awg5204_tm hardware/sequence/waveform).

Contract models describe channel/marker/run configuration (they cross the
plugin → capability boundary); waveform sample data stays in data-plane
dataclasses that hold ndarrays and are validated by guard functions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, Mapping

import numpy as np
from pydantic import Field

from ..core.contracts import ContractModel
from ..core.errors import InstrumentContractError

#: AWG5200-series waveform length granularity (samples).
WAVEFORM_GRANULARITY = 64
DEFAULT_SAMPLE_RATE_HZ = 1.25e9


class AnalogChannelConfig(ContractModel):
    """Analog output channel configuration (1-based channel index)."""

    channel: Annotated[int, Field(ge=1, le=4)]
    enabled: bool = True
    amplitude_vpp: Annotated[float, Field(ge=0)] = 0.5
    offset_v: float = 0.0


class MarkerConfig(ContractModel):
    """Marker output bound to an analog channel."""

    channel: Annotated[int, Field(ge=1, le=4)]
    marker: Annotated[int, Field(ge=1, le=4)]
    delay_s: float = 0.0
    amplitude_v: float | None = None
    offset_v: float | None = None
    high_level_v: float | None = None
    low_level_v: float | None = None


class TriggerSettings(ContractModel):
    source: Literal["INT", "EXT"] = "INT"
    slope: Literal["POS", "NEG"] | None = None
    level_v: float | None = None
    impedance_ohm: float | None = None
    interval_s: float | None = None


class OutputSetup(ContractModel):
    """Full output-side configuration applied before a run."""

    channels: tuple[AnalogChannelConfig, ...] = Field(min_length=1)
    markers: tuple[MarkerConfig, ...] = ()
    run_mode: Literal["CONT", "TRIG", "GAT"] = "CONT"
    trigger: TriggerSettings = TriggerSettings()


# --- data-plane: waveforms & sequences --------------------------------------

@dataclass(frozen=True, slots=True)
class WaveformDefinition:
    """One named waveform: float samples in [-1, 1] plus 0/1 marker tracks."""

    name: str
    samples: np.ndarray
    markers: Mapping[int, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.samples.ndim != 1:
            raise InstrumentContractError("waveform samples must be one-dimensional")
        if not np.issubdtype(self.samples.dtype, np.floating):
            raise InstrumentContractError("waveform samples must be floating point")
        for idx, data in self.markers.items():
            if idx not in (1, 2, 3, 4):
                raise InstrumentContractError(f"marker index {idx} out of range 1-4")
            if data.shape != self.samples.shape:
                raise InstrumentContractError("marker data must match waveform length")
            if data.dtype != np.uint8:
                raise InstrumentContractError("marker data must be uint8 (0/1)")

    @property
    def length(self) -> int:
        return int(self.samples.size)

    def ensure_granularity(self, granularity: int = WAVEFORM_GRANULARITY) -> "WaveformDefinition":
        if self.length % granularity == 0:
            return self
        pad = granularity - (self.length % granularity)
        return WaveformDefinition(
            name=self.name,
            samples=np.concatenate([self.samples,
                                    np.zeros(pad, dtype=self.samples.dtype)]),
            markers={i: np.concatenate([m, np.zeros(pad, dtype=m.dtype)])
                     for i, m in self.markers.items()},
        )

    def combined_marker_bits(self) -> np.ndarray:
        """Marker bits packed per sample (marker1 → bit7, marker2 → bit6, ...)."""
        packed = np.zeros(self.length, dtype=np.uint8)
        for idx, data in self.markers.items():
            packed |= (data.astype(np.uint8) & 0x1) << (8 - idx)
        return packed


@dataclass(frozen=True, slots=True)
class SequenceStep:
    """One playlist step: track number → waveform name."""

    track_waveforms: Mapping[int, str]
    repeat: int | str = 1              # positive int or "INF"

    def __post_init__(self) -> None:
        for track in self.track_waveforms:
            if track < 1:
                raise InstrumentContractError("track numbers are 1-based")
        if isinstance(self.repeat, str):
            if self.repeat.strip().upper() not in ("INF", "INFINITE"):
                raise InstrumentContractError(f"invalid repeat {self.repeat!r}")
        elif self.repeat < 1:
            raise InstrumentContractError("repeat count must be positive")

    @property
    def repeat_token(self) -> str:
        if isinstance(self.repeat, str):
            return "INF"
        return str(self.repeat)


@dataclass(frozen=True, slots=True)
class SequenceDefinition:
    """A compiled instrument program: waveform dictionary + playlist."""

    name: str
    sample_rate: float
    steps: tuple[SequenceStep, ...]
    waveforms: Mapping[str, WaveformDefinition]
    tracks: int = 4

    def __post_init__(self) -> None:
        if self.tracks < 1:
            raise InstrumentContractError("sequence needs at least one track")
        if not np.isfinite(self.sample_rate) or self.sample_rate <= 0:
            raise InstrumentContractError("sample rate must be positive")
        for name, wf in self.waveforms.items():
            if wf.name != name:
                raise InstrumentContractError("waveform dict keys must match names")
        for step in self.steps:
            for track, name in step.track_waveforms.items():
                if track > self.tracks:
                    raise InstrumentContractError(
                        f"track {track} exceeds track count {self.tracks}")
                if name not in self.waveforms:
                    raise InstrumentContractError(f"undefined waveform {name!r}")


def clip_to_range(samples: np.ndarray, low: float = -1.0, high: float = 1.0) -> np.ndarray:
    return np.clip(samples, low, high)


def normalise_waveforms(waveforms: tuple[WaveformDefinition, ...]) -> tuple[WaveformDefinition, ...]:
    """Clip to [-1, 1] and pad every waveform to the instrument granularity."""
    return tuple(
        WaveformDefinition(name=wf.name, samples=clip_to_range(wf.samples),
                           markers=wf.markers).ensure_granularity()
        for wf in waveforms
    )


def step_sine_waveform(
    name: str,
    frequency_mhz: float,
    duration_us: float,
    *,
    step_time_us: float = 0.0,
    amplitude: float = 1.0,
    phase_rad: float = 0.0,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
) -> WaveformDefinition:
    """A sine that starts after a step delay (migrated helper)."""
    total = int(round(duration_us * sample_rate_hz / 1e6))
    if total <= 0:
        raise ValueError("duration_us must produce at least one sample")
    t = np.arange(total, dtype=np.float64) / sample_rate_hz
    mask = (t >= max(0.0, step_time_us) / 1e6).astype(np.float64)
    samples = amplitude * mask * np.sin(2.0 * np.pi * frequency_mhz * 1e6 * t + phase_rad)
    return WaveformDefinition(name=name,
                              samples=clip_to_range(samples).astype(np.float32))
