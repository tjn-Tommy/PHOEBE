"""DAQ (analog input) domain models (migrated from TPA_experiment daq_module)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pydantic import Field
from typing import Annotated

from ..core.contracts import AwareDatetime, ContractModel, InstrumentId, Seconds


class AnalogReadConfig(ContractModel):
    """One untriggered finite acquisition: arm, sample for ``duration_s``, stop."""

    channel: str = "ai0"
    sample_rate_hz: Annotated[float, Field(gt=0)] = 100.0
    duration_s: Seconds = 0.05
    hold_s: Annotated[float, Field(ge=0)] = 0.1     # settle before sampling
    min_val_v: float = -0.010
    max_val_v: float = 0.050
    timeout_s: Seconds = 30.0


class AnalogReadMeta(ContractModel):
    instrument_id: InstrumentId
    config: AnalogReadConfig
    t_wall: AwareDatetime
    t_mono_ns: int


@dataclass(frozen=True, slots=True)
class AnalogTrace:
    """Data-plane object: raw voltage samples of one finite acquisition."""

    values: np.ndarray
    meta: AnalogReadMeta

    @property
    def mean(self) -> float:
        return float(self.values.mean())

    @property
    def std(self) -> float:
        return float(self.values.std())
