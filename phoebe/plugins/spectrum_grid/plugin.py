"""Grid-scan demo plugin: sweep uniform SLM levels, acquire a spectrum per
point via the declarative ``grid_scan`` helper (refactor.md §11.2)."""
from __future__ import annotations

import numpy as np
from pydantic import Field

from ...api import (
    ContractModel,
    Depends,
    PatternModulator,
    Plugin,
    RunContext,
    ScanAxis,
    SpectrumAnalyzer,
    grid_scan,
    on_command,
)
from ...domain.spectrum import SpectrumScanConfig, TraceRequest


class GridScanConfig(ContractModel):
    levels: tuple[float, ...] = Field(default=(0, 128, 256, 384, 512),
                                      min_length=1)
    scan: SpectrumScanConfig = SpectrumScanConfig(
        center_nm=778.0, span_nm=8.0, points=501)


class SpectrumGridPlugin(Plugin):
    plugin_id = "org.lab.spectrum_grid"
    config_type = GridScanConfig

    @on_command("start_grid_scan")
    async def run(
        self,
        config: GridScanConfig,
        ctx: RunContext,
        slm: PatternModulator = Depends(role="primary_slm"),
        osa: SpectrumAnalyzer = Depends(role="main_osa"),
    ) -> None:
        spec = slm.get_frame_spec()
        request = TraceRequest(scan=config.scan)

        async def apply(point: dict[str, float]) -> None:
            level = int(point["slm_level"])
            frame = np.full((spec.height, spec.width), level, dtype=np.uint16)
            await slm.display_pattern(frame, context=ctx)

        async def acquire():
            return await osa.acquire_trace(request, context=ctx)

        total = await grid_scan(
            ctx,
            axes=[ScanAxis(name="slm_level", values=config.levels)],
            apply=apply,
            acquire=acquire,
        )
        ctx.log.info("grid scan complete", points=total)
