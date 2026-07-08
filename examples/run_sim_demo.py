"""End-to-end offline demo: build the sim runtime, run a short TPA search,
then a grid scan, and print where the run data landed.

Usage (from the repo root):

    python examples/run_sim_demo.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoebe.app.bootstrap import build_runtime
from phoebe.core.config import load_app_config
from phoebe.core.gateway import CommandEnvelope
from phoebe.plugins import load_builtin_plugins


async def main() -> None:
    load_builtin_plugins()
    config = load_app_config(Path(__file__).parents[1] / "config" / "sim.toml")
    runtime = await build_runtime(config)

    # live progress from the observation bus (what a UI panel would consume)
    sub = runtime.bus.subscribe(["progress", "run_state"])

    async def watch() -> None:
        async for ev in sub:
            if ev.event_type == "progress":
                print(f"  step {ev.step}/{ev.total}  {ev.metrics}")
            else:
                print(f"[state] {ev.state} ({ev.reason})")

    watcher = asyncio.create_task(watch())

    print("=== TPA multiplier search (sim) ===")
    ack = await runtime.gateway.submit(CommandEnvelope(
        command_id="demo-1", command="start_tpa_run",
        payload={"max_steps": 10, "seed": 7,
                 "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 501}},
    ))
    if not ack.accepted:
        raise SystemExit(f"dispatch rejected: {ack.reason}")
    await runtime.task_manager.wait(ack.task_id)

    print("=== SLM-level grid scan (sim) ===")
    ack = await runtime.gateway.submit(CommandEnvelope(
        command_id="demo-2", command="start_grid_scan",
        payload={"levels": [0, 128, 256, 384, 512],
                 "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 301}},
    ))
    await runtime.task_manager.wait(ack.task_id)

    watcher.cancel()
    runs_root = Path(runtime.config.storage.runs_root).resolve()
    print(f"\nrun directories under {runs_root}:")
    for run_dir in sorted(runs_root.iterdir()):
        files = ", ".join(sorted(p.name for p in run_dir.iterdir()))
        print(f"  {run_dir.name}: {files}")

    await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
