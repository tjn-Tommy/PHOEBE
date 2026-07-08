"""Simulation backends: physically self-consistent mocks (refactor.md §14.2)."""

from .context import SimContext, TpaPhysicsModel  # noqa: F401
from .controllers import (  # noqa: F401
    SimAnalogInput,
    SimOscilloscope,
    SimPatternModulator,
    SimSpectrumAnalyzer,
    SimWaveformGenerator,
    register_sim_factories,
)
