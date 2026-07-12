"""L4 closed loop (refactor.md §14.3): the full plugin → SLM → OSA → writer
chain runs offline under backend="sim"; DI, leases, bus, HDF5 storage and the
cleanup path are all exercised together.  This is the CI gate."""
from __future__ import annotations

import asyncio
import json
import uuid

import h5py
import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.commands import AckCode
from phoebe.core.config import parse_app_config
from phoebe.core.events import RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

SLM_H, SLM_W = 60, 80          # tiny sim panel keeps the test fast


def _sim_config(runs_root: str) -> dict:
    return {
        "mode": "dev",
        "storage": {"runs_root": runs_root},
        "instruments": [
            {"instrument_id": "slm.primary", "kind": "pattern_modulator",
             "vendor": "santec", "model": "slm-200", "role": "primary_slm",
             "backend": "sim",
             "connection": {"transport": "vendor_dll", "dll_path": "unused"},
             "options": {"settle_ms": 1.0, "height": SLM_H, "width": SLM_W,
                         "levels": 1024, "lut_id": "sim_lut"}},
            {"instrument_id": "osa.main", "kind": "spectrum_analyzer",
             "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
             "backend": "sim",
             "connection": {"transport": "tcp", "host": "sim", "port": 10001}},
        ],
        "plugins": {
            "org.lab.tpa_multiplier": {"bindings": {"slm": "primary_slm",
                                                    "osa": "main_osa"}},
            "org.lab.spectrum_grid": {"bindings": {"slm": "primary_slm",
                                                   "osa": "main_osa"}},
        },
    }


@pytest.fixture()
async def runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


def _tpa_payload(steps: int = 8) -> dict:
    return {
        "max_steps": steps, "seed": 42,
        "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 201},
        "mask_spot_check_every": 4,
    }


async def _submit(runtime, command: str, payload: dict):
    # unique ids: the command ledger replays reused ids by design (plan §6.4)
    ack = await runtime.gateway.submit(CommandEnvelope(
        command_id=f"cmd-{uuid.uuid4().hex[:8]}", command=command,
        payload=payload))
    return ack


async def test_tpa_run_completes_with_full_run_directory(runtime, tmp_path):
    sub = runtime.bus.subscribe(["progress", "data_pointer", "run_state"],
                                maxsize=4096)
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(8))
    assert ack.accepted, ack.reason
    state = await runtime.task_manager.wait(ack.task_id)
    assert state is RunState.COMPLETED

    run_dirs = [p for p in (tmp_path / "runs").iterdir()
                if p.is_dir() and not p.name.startswith(".")]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    # §10.5 reproducibility checklist
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["plugin_id"] == "org.lab.tpa_multiplier"
    assert manifest["config_hash"]
    assert manifest["instruments"]["slm.primary"]["options"]["lut_id"] == "sim_lut"
    assert manifest["instruments"]["slm.primary"]["options"]["settle_ms"] == 1.0
    assert (run_dir / "baseline_pre.json").exists()
    assert (run_dir / "baseline_post.json").exists()
    assert (run_dir / "experiment.jsonl").stat().st_size > 0

    # data plane: every trace landed in HDF5 (nothing depended on the bus)
    with h5py.File(run_dir / "artifacts.h5", "r") as h5:
        assert h5["traces/spectrum"].shape == (8, 201)
        assert h5["masks/spot_check"].shape == (2, SLM_H, SLM_W)
        attrs = json.loads(h5["traces/spectrum__attrs"][0].decode())
        assert attrs["scan"]["center_nm"] == 778.0

    metrics = [json.loads(line) for line in
               (run_dir / "metrics.jsonl").read_text().splitlines()]
    assert len(metrics) == 8
    assert all("peak_dbm" in m["values"] for m in metrics)
    if (run_dir / "metrics.parquet").exists():
        import pyarrow.parquet as pq
        assert pq.read_table(run_dir / "metrics.parquet").num_rows == 8

    # control plane: pointer events with previews, no drops, no leaks
    events = []
    while (ev := sub.get_nowait()) is not None:
        events.append(ev)
    pointers = [e for e in events if getattr(e, "event_type", "") == "data_pointer"]
    assert pointers and pointers[0].preview is not None
    assert len(pointers[0].preview.y_dbm) <= 256
    assert runtime.bus.total_dropped() == 0
    assert runtime.device_manager.active_lease_count() == 0


async def test_grid_scan_via_sweep_helper(runtime, tmp_path):
    payload = {"levels": [0, 256, 512],
               "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}
    ack = await _submit(runtime, "start_grid_scan", payload)
    assert ack.accepted, ack.reason
    state = await runtime.task_manager.wait(ack.task_id)
    assert state is RunState.COMPLETED
    run_dir = next(p for p in (tmp_path / "runs").iterdir()
                   if p.is_dir() and not p.name.startswith("."))
    with h5py.File(run_dir / "artifacts.h5", "r") as h5:
        assert h5["traces/grid_scan"].shape == (3, 101)


async def test_second_dispatch_rejected_busy_while_running(runtime):
    ack1 = await _submit(runtime, "start_tpa_run", _tpa_payload(20))
    assert ack1.accepted
    await asyncio.sleep(0.05)
    ack2 = await _submit(runtime, "start_tpa_run", _tpa_payload(2))
    assert not ack2.accepted
    assert ack2.code is AckCode.DEVICE_BUSY        # typed code, zero prose
    runtime.task_manager.request_cancel(ack1.task_id)
    await runtime.task_manager.wait(ack1.task_id)


async def test_pause_resume_cancel_lifecycle(runtime):
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(10_000))
    tm = runtime.task_manager
    await asyncio.sleep(0.1)

    tm.request_pause(ack.task_id)
    for _ in range(200):
        if tm.state_of(ack.task_id) is RunState.PAUSED:
            break
        await asyncio.sleep(0.01)
    assert tm.state_of(ack.task_id) is RunState.PAUSED

    tm.request_resume(ack.task_id)
    await asyncio.sleep(0.05)
    assert tm.state_of(ack.task_id) is RunState.RUNNING

    # cancel via the gateway built-in, like a UI would
    ack2 = await runtime.gateway.submit(CommandEnvelope(
        command_id="c2", command="cancel",
        payload={"task_id": str(ack.task_id)}))
    assert ack2.accepted
    state = await tm.wait(ack.task_id)
    assert state is RunState.ABORTED
    assert runtime.device_manager.active_lease_count() == 0


async def test_invalid_payload_rejected_at_dispatch(runtime):
    ack = await _submit(runtime, "start_tpa_run",
                        {"max_steps": "not-an-int"})
    assert not ack.accepted
    assert ack.code is AckCode.INVALID_PAYLOAD
    assert "invalid payload" in ack.reason


async def test_unknown_command_rejected(runtime):
    ack = await _submit(runtime, "warp_drive", {})
    assert not ack.accepted
    assert ack.code is AckCode.UNKNOWN_COMMAND


async def test_sim_physics_mask_drives_spectrum(runtime):
    """Uniform mask (coherent) must beat a random mask — the optimizer's signal."""
    import numpy as np
    from phoebe.core.capability import SystemContext
    from phoebe.domain.spectrum import SpectrumScanConfig, TraceRequest

    dm = runtime.device_manager
    slm = dm.controller("slm.primary")
    osa = dm.controller("osa.main")
    ctx = SystemContext()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=101)

    uniform = np.zeros((SLM_H, SLM_W), dtype=np.uint16)
    await slm.display_pattern(uniform, context=ctx)
    coherent = (await osa.acquire_trace(TraceRequest(scan=scan), context=ctx)).peak_dbm

    rng = np.random.default_rng(0)
    random_mask = rng.integers(0, 1024, size=(SLM_H, SLM_W), dtype=np.uint16)
    await slm.display_pattern(random_mask, context=ctx)
    incoherent = (await osa.acquire_trace(TraceRequest(scan=scan), context=ctx)).peak_dbm

    assert coherent > incoherent + 10.0       # >10 dB contrast
