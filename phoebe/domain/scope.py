"""Oscilloscope domain models (migrated from TPA_experiment scope_module)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

import numpy as np
from pydantic import Field

from ..core.contracts import AwareDatetime, ContractModel, InstrumentId, Seconds


class ChannelConfig(ContractModel):
    channel: Annotated[int, Field(ge=1, le=4)]
    enabled: bool = True
    scale_v_per_div: float | None = None
    offset_v: float | None = None
    coupling: Literal["DC", "DCLimit", "AC"] | None = None
    bandwidth: Literal["FULL", "B800", "B200", "B20"] | None = None
    digital_filter_cutoff_hz: float | None = None


class TriggerConfig(ContractModel):
    mode: Literal["AUTO", "NORMal", "FREerun"] = "NORMal"
    source: Literal["CHANnel1", "CHANnel2", "CHANnel3", "CHANnel4",
                    "EXTernanalog"] = "CHANnel1"
    level_v: float | None = None
    slope: Literal["POSitive", "NEGative"] = "POSitive"


class AcquisitionConfig(ContractModel):
    channels: tuple[ChannelConfig, ...] = Field(min_length=1)
    trigger: TriggerConfig = TriggerConfig()
    time_range_s: Seconds = 1.0
    record_length: Annotated[int, Field(ge=1)] | None = None
    acquisition_count: Annotated[int, Field(ge=1)] = 1
    decimation: Literal["SAMPle", "PDETect", "HRESolution", "RMS"] | None = None
    arithmetics: Literal["OFF", "AVERage", "ENVelope"] | None = None
    post_trigger_window: bool = True


class MonitorSettings(ContractModel):
    """Settle-then-read averaged scalar sample (TPA encoder feedback path)."""

    channel: Annotated[int, Field(ge=1, le=4)] = 1
    hold_s: Annotated[float, Field(ge=0)] = 0.1     # settle after pattern change
    gate_start_s: float | None = None
    gate_stop_s: float | None = None


class MonitorSample(ContractModel):
    value: float
    std: float
    index: int = 0
    t_wall: AwareDatetime
    t_mono_ns: int


class WaveformMeta(ContractModel):
    instrument_id: InstrumentId
    channel: int
    x_start_s: float
    x_stop_s: float
    record_length: int
    values_per_sample: int = 1
    t_wall: AwareDatetime
    t_mono_ns: int


@dataclass(frozen=True, slots=True)
class ScopeWaveform:
    """Data-plane object: samples stay in-process, body goes to RunWriter."""

    values: np.ndarray            # float32/float64, shape (N,) or (N, 2) for peak-detect
    meta: WaveformMeta

    def time_axis_s(self) -> np.ndarray:
        n = self.meta.record_length
        return np.linspace(self.meta.x_start_s, self.meta.x_stop_s, n)
