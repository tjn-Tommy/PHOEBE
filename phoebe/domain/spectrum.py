"""Spectrum-analyzer domain models (refactor.md §3.3).

``SpectrumScanConfig``/``TraceRequest`` are contract models; ``SpectrumTrace``
is a data-plane object — it may hold ndarrays, never travels on the bus, and
its array body goes through the RunWriter while its metadata (a contract
model) rides along as HDF5 attrs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from ..core.contracts import (
    AwareDatetime,
    ContractModel,
    Dbm,
    InstrumentId,
    Nanometer,
)
from ..core.events import TracePreview

Sensitivity = Literal["norm", "mid", "high1", "high2", "high3"]


class SpectrumScanConfig(ContractModel):
    center_nm: Nanometer
    span_nm: Annotated[float, Field(gt=0, le=1500)]
    points: Annotated[int, Field(ge=11, le=100_001)]
    resolution_nm: Annotated[float, Field(gt=0)] = 0.02
    sensitivity: Sensitivity = "mid"
    average_count: Annotated[int, Field(ge=1, le=999)] = 1
    reference_level_dbm: Dbm = -20.0

    @model_validator(mode="after")
    def _resolution_vs_span(self) -> "SpectrumScanConfig":
        if self.resolution_nm > self.span_nm:
            raise ValueError("resolution_nm must not exceed span_nm")
        return self

    def wavelength_axis_nm(self) -> np.ndarray:
        half = self.span_nm / 2.0
        return np.linspace(self.center_nm - half, self.center_nm + half,
                           self.points, dtype=np.float64)


class TraceRequest(ContractModel):
    scan: SpectrumScanConfig
    trace_name: Literal["TRA", "TRB", "TRC"] = "TRA"


class Peak(ContractModel):
    wavelength_nm: Nanometer
    power_dbm: Dbm


class PeakSearchRequest(ContractModel):
    threshold_dbm: Dbm = -60.0
    max_peaks: Annotated[int, Field(ge=1, le=64)] = 8


class TraceMeta(ContractModel):
    instrument_id: InstrumentId
    scan: SpectrumScanConfig
    trace_name: str = "TRA"
    averages: int = 1
    t_wall: AwareDatetime
    t_mono_ns: int


@dataclass(frozen=True, slots=True)
class SpectrumTrace:
    """Data-plane object: flows only inside the process; body → RunWriter."""

    x_nm: np.ndarray          # float64, shape (N,)
    y_dbm: np.ndarray         # float32, shape (N,)
    meta: TraceMeta

    def preview(self, n: int = 256) -> TracePreview:
        idx = np.linspace(0, len(self.x_nm) - 1, min(n, len(self.x_nm))).astype(int)
        return TracePreview(
            x_nm=[float(v) for v in self.x_nm[idx]],
            y_dbm=[float(v) for v in self.y_dbm[idx]],
        )

    @property
    def peak_dbm(self) -> float:
        return float(self.y_dbm.max())


def average_traces_dbm(spectra: list[np.ndarray]) -> np.ndarray:
    """True power average of dBm spectra (in the linear domain, not log)."""
    stacked = np.vstack(spectra)
    linear = 10.0 ** (stacked / 10.0)
    return (10.0 * np.log10(linear.mean(axis=0))).astype(np.float32)
