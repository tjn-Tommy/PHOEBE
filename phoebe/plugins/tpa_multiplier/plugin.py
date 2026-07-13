"""TPA multiplier encoding-search plugin (refactor.md §11.1).

The migrated form of the TPA experiment's optimization loop skeleton: random
mask search against the OSA peak.  Note what the platform now guarantees —
``display_pattern`` returns settled, checkpoints make the loop pausable /
cancellable / heartbeat-visible, bulk traces go to the RunWriter with only
pointers + previews on the bus.

The deep optimization strategies from TPA_experiment (CMA-ES etc. in
slm_module/optimization.py) can be ported into ``propose_mask`` variants
incrementally; the plugin surface stays identical.
"""
from __future__ import annotations

from typing import Annotated

import numpy as np
from pydantic import Field

from ...api import (
    ContractModel,
    Depends,
    MaskRecipe,
    PatternModulator,
    Plugin,
    RunContext,
    SpectrumAnalyzer,
    on_command,
)
from ...domain.pattern import make_uniform_random
from ...domain.spectrum import SpectrumScanConfig, TraceRequest


class TPAConfig(ContractModel):
    max_steps: Annotated[int, Field(ge=1, le=1_000_000)] = 100
    seed: int = 0
    scan: SpectrumScanConfig = SpectrumScanConfig(
        center_nm=778.0, span_nm=8.0, points=1001)
    trace_name: str = "TRA"
    mask_spot_check_every: Annotated[int, Field(ge=1)] = 100


class TPAMultiplierPlugin(Plugin):
    plugin_id = "org.lab.tpa_multiplier"
    config_type = TPAConfig

    @on_command("start_tpa_run")
    async def run(
        self,
        config: TPAConfig,
        ctx: RunContext,
        slm: PatternModulator = Depends(role="primary_slm"),
        osa: SpectrumAnalyzer = Depends(role="main_osa"),
    ) -> None:
        ctx.log.info("TPA encoding loop starting", max_steps=config.max_steps)
        spec = slm.get_frame_spec()
        rng = np.random.default_rng(config.seed)
        request = TraceRequest(scan=config.scan, trace_name="TRA")
        best_dbm = -np.inf

        for step in range(config.max_steps):
            await ctx.checkpoint("tpa_step", step=step)      # pause/cancel/heartbeat

            recipe = MaskRecipe(generator="uniform_random", version="1",
                                seed=config.seed, params={"step": step})
            frame = make_uniform_random(rng, spec)           # uint16 native levels
            await slm.display_pattern(frame, context=ctx)    # returns settled

            trace = await osa.acquire_trace(request, context=ctx)

            pointer = await ctx.writer.append_array(         # data plane (backpressure)
                "traces/spectrum", trace.y_dbm, attrs=trace.meta)
            if step % config.mask_spot_check_every == 0:     # spot-check raw frames
                await ctx.writer.append_array("masks/spot_check", frame,
                                              attrs=recipe)
            peak = trace.peak_dbm
            best_dbm = max(best_dbm, peak)
            await ctx.writer.append_metrics(step=step, peak_dbm=peak,
                                            best_dbm=best_dbm)
            ctx.emit_progress(step=step, total=config.max_steps,
                              metrics={"peak_dbm": peak, "best_dbm": best_dbm},
                              pointer=pointer, preview=trace.preview())

        ctx.log.info("TPA loop finished", best_dbm=best_dbm)
