"""phoebe.api — the sanctioned import surface for plugin authors (plan B5).

Everything a plugin may touch is re-exported here, versioned by
``PLUGIN_API_VERSION``.  Plugin code imports **only** from this module (plus
``phoebe.domain`` data types, numpy/pydantic): never from ``phoebe.core``,
``phoebe.transports``, instrument implementations, or the UI — the
import-linter contract and the conformance suite both enforce it, so kernel
refactors can never break a conforming plugin.

Typical plugin::

    from phoebe.api import (ContractModel, Depends, PatternModulator, Plugin,
                            RunContext, on_command, register)

    class MyConfig(ContractModel):
        steps: int = 10

    @register(plugin_id="org.lab.my_experiment")
    class MyPlugin(Plugin):
        config_type = MyConfig

        @on_command("start_my_experiment")
        async def run(self, config: MyConfig, ctx: RunContext,
                      slm: PatternModulator = Depends(role="primary_slm")) -> None:
            await ctx.checkpoint("step")          # pause/cancel/heartbeat
            ...
"""
from __future__ import annotations

from ..contracts.base import ContractModel, validate_boundary
from ..contracts.errors import CancelledByUser, InstrumentError
from ..contracts.plugin import PluginManifest, PluginStatusView
from ..core.di import Depends
from ..core.plugin import (
    PLUGIN_API_VERSION,
    Plugin,
    PluginRegistry,
    load_plugin_directory,
    on_command,
    register,
)
from ..core.sweep import ScanAxis, grid_scan
from ..core.task_manager import RunContext
from ..core.writer import MaskRecipe
from ..instruments.protocols import (
    AnalogInput,
    Oscilloscope,
    PatternModulator,
    SpectrumAnalyzer,
    WaveformGenerator,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "AnalogInput",
    "CancelledByUser",
    "ContractModel",
    "Depends",
    "InstrumentError",
    "MaskRecipe",
    "Oscilloscope",
    "PatternModulator",
    "Plugin",
    "PluginManifest",
    "PluginRegistry",
    "PluginStatusView",
    "RunContext",
    "ScanAxis",
    "SpectrumAnalyzer",
    "WaveformGenerator",
    "grid_scan",
    "load_plugin_directory",
    "on_command",
    "register",
    "validate_boundary",
]
