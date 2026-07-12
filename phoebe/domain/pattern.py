"""Pattern-modulator (SLM) domain models (refactor.md §4.4, §10.3).

Frames are stored and transported at the panel's native quantization depth
(SLM-200: 10-bit → uint16, values 0..1023) with a LUT reference — never as
float radians.  ``validate_frame`` is the guard function that stands in for
Pydantic where ndarrays are involved.
"""
from __future__ import annotations

from typing import Annotated
from collections.abc import Sequence

import numpy as np
from pydantic import Field

from ..core.contracts import ContractModel, Millisecond
from ..core.errors import InstrumentContractError


class PatternSpec(ContractModel):
    height: Annotated[int, Field(gt=0)]   # SLM-200: 1200
    width: Annotated[int, Field(gt=0)]    # SLM-200: 1920
    levels: Annotated[int, Field(gt=1)]   # 10-bit → 1024


class SlmOptions(ContractModel):
    """Validated from InstrumentConfig.options by the SLM factory (two-stage)."""

    settle_ms: Millisecond = 50.0         # written into RunManifest → reproducible
    lut_id: str = ""                      # phase-calibration LUT reference
    display_no: Annotated[int, Field(ge=1)] = 1
    rate120: bool = False
    height: Annotated[int, Field(gt=0)] = 1200
    width: Annotated[int, Field(gt=0)] = 1920
    levels: Annotated[int, Field(gt=1)] = 1024

    def spec(self) -> PatternSpec:
        return PatternSpec(height=self.height, width=self.width, levels=self.levels)


def validate_frame(frame: np.ndarray, spec: PatternSpec) -> None:
    """ndarray guard driven by the spec contract (Pydantic can't check arrays)."""
    if frame.ndim != 2 or frame.shape != (spec.height, spec.width):
        raise InstrumentContractError(
            f"frame shape {frame.shape} != ({spec.height}, {spec.width})"
        )
    if frame.dtype != np.uint16:
        raise InstrumentContractError(
            f"frame dtype {frame.dtype} — must be uint16 native quantization levels"
        )
    if int(frame.max(initial=0)) >= spec.levels:
        raise InstrumentContractError(
            f"frame contains values >= {spec.levels} (native quantization exceeded)"
        )


# --- mask generators (migrated from TPA_experiment slm_module/generator) ----

def make_vertical_window(spec: PatternSpec, x_start: int, level: int,
                         window_px: int = 5, background_level: int = 0) -> np.ndarray:
    """A vertical band [x_start, x_start+window_px) at ``level`` over background."""
    _check_level(spec, level)
    _check_level(spec, background_level)
    if not 0 <= x_start < spec.width:
        raise ValueError(f"x_start must be in 0..{spec.width - 1}")
    if window_px <= 0:
        raise ValueError("window_px must be positive")
    frame = np.full((spec.height, spec.width), background_level, dtype=np.uint16)
    frame[:, x_start:min(spec.width, x_start + window_px)] = level
    return frame


def make_x_segments(spec: PatternSpec,
                    segments: Sequence[tuple[int, int, int]],
                    *, background_level: int = 0) -> np.ndarray:
    """Non-overlapping vertical bands (x_start, x_end, level) over background."""
    _check_level(spec, background_level)
    if not segments:
        raise ValueError("segments must not be empty")
    ordered = sorted(segments, key=lambda s: s[0])
    for (a_start, a_end, _), (b_start, b_end, _) in zip(ordered, ordered[1:],
                                                        strict=False):
        if b_start < a_end:
            raise ValueError(f"segments overlap: [{a_start},{a_end}) and [{b_start},{b_end})")
    frame = np.full((spec.height, spec.width), background_level, dtype=np.uint16)
    for x_start, x_end, level in ordered:
        _check_level(spec, level)
        if not (0 <= x_start < x_end <= spec.width):
            raise ValueError(f"segment [{x_start},{x_end}) outside 0..{spec.width}")
        frame[:, x_start:x_end] = level
    return frame


def make_equal_x_segments(spec: PatternSpec, levels: Sequence[int]) -> np.ndarray:
    """Divide the x axis into len(levels) equal bands, one level each."""
    if not levels:
        raise ValueError("levels must not be empty")
    if len(levels) > spec.width:
        raise ValueError("number of bands cannot exceed panel width")
    edges = [round(i * spec.width / len(levels)) for i in range(len(levels) + 1)]
    segments = [(edges[i], edges[i + 1], int(level)) for i, level in enumerate(levels)]
    return make_x_segments(spec, segments)


def make_uniform_random(rng: np.random.Generator, spec: PatternSpec) -> np.ndarray:
    return rng.integers(0, spec.levels, size=(spec.height, spec.width),
                        dtype=np.uint16)


def _check_level(spec: PatternSpec, level: int) -> None:
    if not 0 <= int(level) < spec.levels:
        raise ValueError(f"level must be in 0..{spec.levels - 1}")
