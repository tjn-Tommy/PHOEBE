"""Shared physical state for sim devices (refactor.md §14.2).

The mock does not return canned traces: the spectrum is computed from the
CURRENT SLM mask, so the whole plugin → SLM → OSA → optimizer loop closes
offline and optimizer behaviour can be tuned before touching hardware.
"""
from __future__ import annotations

import numpy as np

from ...domain.spectrum import SpectrumScanConfig


class TpaPhysicsModel:
    """Toy-but-self-consistent TPA multiplier model.

    The pump's spectral components acquire the phase written on the SLM; the
    two-photon signal at the center wavelength scales with the coherence of
    the applied phase mask ``|<exp(i·φ)>|²`` — a uniform mask gives maximum
    signal, a random mask destroys it.  This preserves exactly the property an
    optimizer needs: the observable responds monotonically to mask quality.
    """

    def __init__(self, *, levels: int = 1024, peak_mw: float = 1.0,
                 noise_floor_dbm: float = -75.0, shot_noise_db: float = 0.3,
                 base_sweep_time_s: float = 0.02, seed: int = 1234) -> None:
        self.levels = levels
        self.peak_mw = peak_mw
        self.noise_floor_dbm = noise_floor_dbm
        self.shot_noise_db = shot_noise_db
        self.base_sweep_time_s = base_sweep_time_s
        self._rng = np.random.default_rng(seed)

    def coherence(self, mask: np.ndarray | None) -> float:
        if mask is None:
            return 1.0                              # nothing applied: flat phase
        phase = mask.astype(np.float64) * (2.0 * np.pi / self.levels)
        return float(np.abs(np.exp(1j * phase).mean()) ** 2)

    def spectrum_dbm(self, mask: np.ndarray | None,
                     scan: SpectrumScanConfig) -> np.ndarray:
        x = scan.wavelength_axis_nm()
        sigma_nm = max(scan.span_nm / 20.0, scan.resolution_nm)
        signal_mw = self.peak_mw * self.coherence(mask)
        floor_mw = 10.0 ** (self.noise_floor_dbm / 10.0)
        y_mw = floor_mw + signal_mw * np.exp(
            -0.5 * ((x - scan.center_nm) / sigma_nm) ** 2)
        return (10.0 * np.log10(y_mw)).astype(np.float32)

    def add_shot_noise(self, y_dbm: np.ndarray,
                       scan: SpectrumScanConfig) -> np.ndarray:
        noise = self._rng.normal(0.0, self.shot_noise_db / np.sqrt(scan.average_count),
                                 size=y_dbm.shape)
        return (y_dbm + noise).astype(np.float32)

    def sweep_time_s(self, scan: SpectrumScanConfig) -> float:
        return self.base_sweep_time_s * (scan.points / 1001.0) * scan.average_count

    def detector_volts(self, mask: np.ndarray | None) -> float:
        """Photodiode-style scalar for scope/DAQ feedback paths."""
        return 0.005 + 0.045 * self.coherence(mask)


class SimContext:
    """Shared physical state between the sim devices of one process."""

    def __init__(self, model: TpaPhysicsModel | None = None) -> None:
        self.model = model or TpaPhysicsModel()
        self.current_mask: np.ndarray | None = None
