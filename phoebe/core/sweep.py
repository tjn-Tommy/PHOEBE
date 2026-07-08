"""Sweep helpers: declarative scan primitives (refactor.md §11.2).

90% of experiments are "nested sweep × acquire × persist"; ``grid_scan``
bundles checkpointing, cancellation, writing and throttled progress so a
300-line for-loop becomes a 20-line declaration.
"""
from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Awaitable, Callable, Sequence

from pydantic import Field

from .contracts import ContractModel

if TYPE_CHECKING:
    from ..domain.spectrum import SpectrumTrace
    from .task_manager import RunContext


class ScanAxis(ContractModel):
    name: str
    values: tuple[float, ...] = Field(min_length=1)


class ScanPointMeta(ContractModel):
    index: int
    point: dict[str, float]


async def grid_scan(
    ctx: "RunContext",
    axes: Sequence[ScanAxis],
    apply: Callable[[dict[str, float]], Awaitable[None]],       # set one grid point
    acquire: Callable[[], "Awaitable[SpectrumTrace]"],          # measure one point
    *,
    dataset: str = "traces/grid_scan",
) -> int:
    """Cartesian sweep over ``axes``; returns the number of completed points."""
    points = list(itertools.product(*(axis.values for axis in axes)))
    total = len(points)
    for i, combo in enumerate(points):
        point = {axis.name: value for axis, value in zip(axes, combo)}
        await ctx.checkpoint("scan_point", index=i,
                             **{k: float(v) for k, v in point.items()})
        await apply(point)
        trace = await acquire()
        pointer = await ctx.writer.append_array(
            dataset, trace.y_dbm, attrs=ScanPointMeta(index=i, point=point),
        )
        ctx.emit_progress(step=i, total=total, pointer=pointer,
                          preview=trace.preview())
    return total
