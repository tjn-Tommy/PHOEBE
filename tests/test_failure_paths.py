"""Failure-path suite (evolution plan Phase A, PR A-6).

Every test here exercises a path that previously hung, zombied or corrupted
state: writer death (C1), shutdown mid-run (C2), pause→cancel race (H1),
pre-RUNNING failures (H2/H3), reaper double-release (H4), paused-run reaping
(H5), queue stall after reap (H11), plus suspender auto-pause/resume."""
from __future__ import annotations

import asyncio
import json
import uuid

import h5py
import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.core.config import parse_app_config
from phoebe.core.contracts import ContractModel, InstrumentId, TaskId, timestamps
from phoebe.core.di import Depends
from phoebe.core.errors import WriterFailedError
from phoebe.core.events import DeviceHealthEvent, RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.core.plugin import Plugin, on_command, register
from phoebe.core.task_manager import RunContext
from phoebe.core.writer import RunWriter
from phoebe.instruments.protocols import PatternModulator
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

SLM_H, SLM_W = 60, 80


# --------------------------------------------------------------- test plugins
class FailingConfig(ContractModel):
    fail_at_step: int = 1


@register(plugin_id="test.failing")
class FailingPlugin(Plugin):
    config_type = FailingConfig

    @on_command("start_failing_run")
    async def run(self, config: FailingConfig, ctx: RunContext,
                  slm: PatternModulator = Depends(role="primary_slm")) -> None:
        for step in range(config.fail_at_step + 1):
            await ctx.checkpoint("failing", step=step)
            if step == config.fail_at_step:
                raise RuntimeError("boom: injected plugin failure")
            await asyncio.sleep(0.005)


class HangConfig(ContractModel):
    pass


@register(plugin_id="test.hanging")
class HangingPlugin(Plugin):
    """Checkpoints once, then parks forever on an un-set event — the exact
    shape of a run whose single awaited operation outlives the lease TTL."""

    config_type = HangConfig

    @on_command("start_hanging_run")
    async def run(self, config: HangConfig, ctx: RunContext,
                  slm: PatternModulator = Depends(role="primary_slm")) -> None:
        await ctx.checkpoint("hang_start")
        await asyncio.Event().wait()          # no heartbeats from here on


# ------------------------------------------------------------------ fixtures
def _sim_config(runs_root: str, **overrides) -> dict:
    cfg = {
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
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture()
async def runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


def _tpa_payload(steps: int) -> dict:
    return {"max_steps": steps, "seed": 1,
            "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}


async def _submit(rt, command: str, payload: dict):
    # unique ids: the command ledger replays reused ids by design (plan §6.4)
    return await rt.gateway.submit(CommandEnvelope(
        command_id=f"cmd-{uuid.uuid4().hex[:8]}", command=command,
        payload=payload))


def _first_run_dir(runs_root):
    return next(p for p in runs_root.iterdir()
                if p.is_dir() and not p.name.startswith("."))


async def _wait_state(tm, task_id, states: set[RunState], timeout: float = 10.0):
    async def poll():
        while True:
            state = tm.state_of(task_id)
            if state in states:
                return state
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(poll(), timeout)


async def _wait_terminal(tm, task_id, timeout: float = 10.0) -> RunState:
    async def poll():
        while True:
            state = tm.state_of(task_id)
            if state.is_terminal:
                return state
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(poll(), timeout)


# ---------------------------------------------------- plugin failure (H14 tests)
async def test_plugin_exception_yields_failed_with_full_cleanup(runtime, tmp_path):
    slm = runtime.device_manager.controller("slm.primary")
    safe_calls: list[str] = []
    orig_safe = slm.safe_state

    async def spy_safe_state():
        safe_calls.append("safe_state")
        await orig_safe()

    slm.safe_state = spy_safe_state
    sub = runtime.bus.subscribe(["run_state", "error"], maxsize=1024)

    ack = await _submit(runtime, "start_failing_run", {"fail_at_step": 2})
    assert ack.accepted, ack.reason
    state = await _wait_terminal(runtime.task_manager, ack.task_id)
    assert state is RunState.FAILED
    await asyncio.sleep(0.05)                 # let the final rebroadcast land

    # failure path did everything cleanup promises (H14 test debt)
    assert safe_calls, "safe_state() was not invoked on the failure path"
    assert runtime.device_manager.active_lease_count() == 0

    events = []
    while (ev := sub.get_nowait()) is not None:
        events.append(ev)
    errors = [e for e in events if e.event_type == "error"]
    assert errors and "boom" in errors[0].message
    states = [e.state for e in events if e.event_type == "run_state"]
    assert RunState.PREPARING in states
    assert RunState.FINALIZING in states
    finals = [e for e in events
              if e.event_type == "run_state" and e.final]   # typed flag (C-1)
    assert finals and finals[-1].state is RunState.FAILED

    # the run directory is still a coherent record
    run_dir = _first_run_dir(tmp_path / "runs")
    assert (run_dir / "run.json").exists()
    assert (run_dir / "baseline_post.json").exists()
    with h5py.File(run_dir / "artifacts.h5", "r") as h5:   # writer closed cleanly
        assert h5 is not None


async def test_stage_failure_reaches_terminal_state_not_zombie(runtime):
    """H2: a failure while still QUEUED/PREPARING must not strand the record."""
    slm = runtime.device_manager.controller("slm.primary")

    async def failing_stage():
        raise RuntimeError("stage exploded")

    slm.stage = failing_stage
    sub = runtime.bus.subscribe(["run_state"], maxsize=256)
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(4))
    assert ack.accepted
    state = await _wait_terminal(runtime.task_manager, ack.task_id)
    assert state is RunState.FAILED
    assert runtime.task_manager.active_tasks() == ()      # no zombie
    assert runtime.device_manager.active_lease_count() == 0

    await asyncio.sleep(0.05)
    states = []
    while (ev := sub.get_nowait()) is not None:
        states.append(ev.state)
    assert RunState.RUNNING not in states                 # failed before RUNNING


async def test_setup_failure_before_writer_releases_leases(runtime, monkeypatch):
    """H3: an OSError before the writer even exists must still finalize."""
    import phoebe.core.task_manager as tm_mod

    def broken_run_dir(root, plugin_id, task_id):
        raise OSError("disk full at run-dir creation")

    monkeypatch.setattr(tm_mod, "new_run_dir", broken_run_dir)
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(4))
    assert ack.accepted
    state = await _wait_terminal(runtime.task_manager, ack.task_id)
    assert state is RunState.FAILED
    assert runtime.device_manager.active_lease_count() == 0


# ------------------------------------------------------- pause / cancel races
async def test_pause_then_cancel_immediately_aborts_not_fails(runtime):
    """H1: cancel arriving inside the pause window must yield ABORTED."""
    tm = runtime.task_manager
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(10_000))
    await asyncio.sleep(0.05)
    # back-to-back on the loop thread: no checkpoint can interleave
    tm.request_pause(ack.task_id)
    tm.request_cancel(ack.task_id)
    state = await _wait_terminal(tm, ack.task_id)
    assert state is RunState.ABORTED
    assert runtime.device_manager.active_lease_count() == 0


async def test_cancel_while_parked_in_paused_checkpoint(runtime):
    tm = runtime.task_manager
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(10_000))
    await asyncio.sleep(0.05)
    tm.request_pause(ack.task_id)
    await _wait_state(tm, ack.task_id, {RunState.PAUSED})
    tm.request_cancel(ack.task_id)
    state = await _wait_terminal(tm, ack.task_id)
    assert state is RunState.ABORTED


async def test_cancel_during_preparing_aborts(runtime):
    """The new PREPARING state accepts a cancel without an illegal transition."""
    slm = runtime.device_manager.controller("slm.primary")
    stage_entered = asyncio.Event()
    release_stage = asyncio.Event()

    async def slow_stage():
        stage_entered.set()
        await release_stage.wait()

    slm.stage = slow_stage
    tm = runtime.task_manager
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(4))
    await asyncio.wait_for(stage_entered.wait(), 5.0)
    tm.request_cancel(ack.task_id)
    release_stage.set()
    state = await _wait_terminal(tm, ack.task_id)
    assert state is RunState.ABORTED
    assert runtime.device_manager.active_lease_count() == 0


# ------------------------------------------------------------ writer death (C1)
async def test_writer_death_fails_run_fast_instead_of_hanging(runtime, monkeypatch):
    def broken_metric(self, line):
        raise OSError("No space left on device")

    monkeypatch.setattr(RunWriter, "_write_metric", broken_metric)
    tm = runtime.task_manager
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(10_000))
    assert ack.accepted
    # the old kernel hangs here forever; the deadline is the regression check
    state = await _wait_terminal(tm, ack.task_id, timeout=15.0)
    assert state is RunState.FAILED
    assert runtime.device_manager.active_lease_count() == 0


async def test_runwriter_open_failure_resolves_producers(tmp_path, monkeypatch):
    """A writer whose files never open must fail producers, not park them."""
    import numpy as np

    def broken_open(self):
        raise OSError("cannot open artifacts.h5")

    monkeypatch.setattr(RunWriter, "_open_files", broken_open)
    failures: list[BaseException] = []
    writer = RunWriter("run_x", tmp_path / "run_x", queue_size=4,
                       compact_parquet=False, on_failure=failures.append)
    writer.start()
    with pytest.raises(WriterFailedError):
        await asyncio.wait_for(
            writer.append_array("traces/spectrum", np.zeros(8)), 5.0)
    assert failures and isinstance(failures[0], OSError)
    await asyncio.wait_for(writer.aclose(), 5.0)          # bounded close still works


# --------------------------------------------------------------- reaper (H4/H5/H11)
async def test_reaper_cancels_hung_run_and_wakes_queue(tmp_path):
    cfg = parse_app_config(_sim_config(
        str(tmp_path / "runs"), dispatch_policy="queue", lease_ttl_s=0.2))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    rt.device_manager.start_reaper(interval_s=0.05)
    try:
        tm = rt.task_manager
        ack1 = await _submit(rt, "start_hanging_run", {})
        assert ack1.accepted
        ack2 = await _submit(rt, "start_tpa_run", _tpa_payload(2))
        assert ack2.accepted and ack2.queued              # blocked behind the SLM

        # H4/H11: reaper must terminate the hung run and start the queued one
        state1 = await _wait_terminal(tm, ack1.task_id, timeout=10.0)
        assert state1 is RunState.FAILED
        assert "reaped" in (tm._records[ack1.task_id].external_cancel_reason or "")
        state2 = await _wait_terminal(tm, ack2.task_id, timeout=10.0)
        assert state2 is RunState.COMPLETED
        assert rt.device_manager.active_lease_count() == 0
    finally:
        await rt.shutdown()


async def test_paused_run_heartbeats_and_survives_the_reaper(tmp_path):
    cfg = parse_app_config(_sim_config(
        str(tmp_path / "runs"), lease_ttl_s=0.3))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    rt.device_manager.start_reaper(interval_s=0.05)
    try:
        tm = rt.task_manager
        ack = await _submit(rt, "start_tpa_run", _tpa_payload(10_000))
        await asyncio.sleep(0.05)
        tm.request_pause(ack.task_id)
        await _wait_state(tm, ack.task_id, {RunState.PAUSED})

        await asyncio.sleep(0.9)                          # 3× the lease TTL
        # H5: still paused, still owning its devices — not reaped
        assert tm.state_of(ack.task_id) is RunState.PAUSED
        assert rt.device_manager.owner_of(InstrumentId("slm.primary")) is not None

        tm.request_resume(ack.task_id)
        await _wait_state(tm, ack.task_id, {RunState.RUNNING})
        tm.request_cancel(ack.task_id)
        state = await _wait_terminal(tm, ack.task_id)
        assert state is RunState.ABORTED
    finally:
        await rt.shutdown()


async def test_release_after_reap_never_pops_new_holder(tmp_path):
    """H4 unit check on the ownership table itself."""
    from phoebe.core.di import ResolvedRequirement

    cfg = parse_app_config(_sim_config(str(tmp_path / "runs"), lease_ttl_s=0.01))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    try:
        dm = rt.device_manager
        req = [ResolvedRequirement("slm", InstrumentId("slm.primary"),
                                   "pattern_modulator")]
        leases1 = dm.try_acquire_all(TaskId("task_a"), req)
        await asyncio.sleep(0.05)                         # let the TTL lapse
        await dm._reap_once()
        assert dm.owner_of(InstrumentId("slm.primary")) is None

        leases2 = dm.try_acquire_all(TaskId("task_b"), req)
        holder = dm.owner_of(InstrumentId("slm.primary"))
        assert holder is not None and holder.holder_task_id == "task_b"

        dm.release(TaskId("task_a"), leases1)             # stale release
        holder = dm.owner_of(InstrumentId("slm.primary"))
        assert holder is not None and holder.holder_task_id == "task_b"
        dm.release(TaskId("task_b"), leases2)
        assert dm.active_lease_count() == 0
    finally:
        await rt.shutdown()


# ---------------------------------------------------------------- shutdown (C2)
async def test_shutdown_mid_run_drains_cleanup(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    tm = rt.task_manager
    ack = await _submit(rt, "start_tpa_run", _tpa_payload(10_000))
    assert ack.accepted
    await asyncio.sleep(0.15)                             # let it produce data

    await asyncio.wait_for(rt.shutdown(), 30.0)

    assert tm.state_of(ack.task_id) is RunState.ABORTED   # cleanup ran to the end
    assert rt.device_manager.active_lease_count() == 0
    run_dir = _first_run_dir(tmp_path / "runs")
    assert (run_dir / "baseline_post.json").exists()      # finally executed
    with h5py.File(run_dir / "artifacts.h5", "r") as h5:  # writer flushed + closed
        assert "traces/spectrum" in h5
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["task_id"] == str(ack.task_id)

    # maintenance gate: the drained TaskManager accepts no new work
    ack2 = await _submit(rt, "start_tpa_run", _tpa_payload(2))
    assert not ack2.accepted
    assert "maintenance" in (ack2.reason or "")


# ------------------------------------------------------------- queue behaviour
async def test_queued_run_starts_after_predecessor_fails(tmp_path):
    cfg = parse_app_config(_sim_config(
        str(tmp_path / "runs"), dispatch_policy="queue"))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    try:
        tm = rt.task_manager
        ack1 = await _submit(rt, "start_failing_run", {"fail_at_step": 3})
        ack2 = await _submit(rt, "start_tpa_run", _tpa_payload(2))
        assert ack2.accepted and ack2.queued
        assert (await _wait_terminal(tm, ack1.task_id)) is RunState.FAILED
        assert (await _wait_terminal(tm, ack2.task_id)) is RunState.COMPLETED
    finally:
        await rt.shutdown()


async def test_cancel_queued_run_aborts_without_resources(tmp_path):
    cfg = parse_app_config(_sim_config(
        str(tmp_path / "runs"), dispatch_policy="queue"))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    try:
        tm = rt.task_manager
        ack1 = await _submit(rt, "start_tpa_run", _tpa_payload(10_000))
        ack2 = await _submit(rt, "start_tpa_run", _tpa_payload(2))
        assert ack2.queued
        tm.request_cancel(ack2.task_id)
        assert tm.state_of(ack2.task_id) is RunState.ABORTED
        tm.request_cancel(ack1.task_id)
        assert (await _wait_terminal(tm, ack1.task_id)) is RunState.ABORTED
    finally:
        await rt.shutdown()


# ------------------------------------------------------------------- suspender
async def test_suspender_pauses_and_resumes_on_health_metric(tmp_path):
    cfg = parse_app_config(_sim_config(
        str(tmp_path / "runs"),
        suspenders=[{"watch_topic": "device_health", "metric": "pump_mw",
                     "min_value": 1.0, "grace_s": 0.05}]))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    try:
        tm = rt.task_manager
        ack = await _submit(rt, "start_tpa_run", _tpa_payload(10_000))
        await asyncio.sleep(0.05)

        def health(value: float) -> DeviceHealthEvent:
            return DeviceHealthEvent(instrument_id=InstrumentId("osa.main"),
                                     status="ok", metrics={"pump_mw": value},
                                     **timestamps())

        rt.bus.publish(health(0.2))                       # out of range → pause
        await _wait_state(tm, ack.task_id, {RunState.PAUSED})

        for _ in range(8):                                # back in range → resume
            rt.bus.publish(health(5.0))
            await asyncio.sleep(0.03)
        await _wait_state(tm, ack.task_id, {RunState.RUNNING})

        tm.request_cancel(ack.task_id)
        assert (await _wait_terminal(tm, ack.task_id)) is RunState.ABORTED
    finally:
        await rt.shutdown()


# ------------------------------------------------------------ record hygiene
async def test_terminal_records_are_evicted(runtime, monkeypatch):
    import phoebe.core.task_manager as tm_mod

    monkeypatch.setattr(tm_mod, "_MAX_TERMINAL_RECORDS", 2)
    tm = runtime.task_manager
    for _ in range(4):
        ack = await _submit(runtime, "start_tpa_run", _tpa_payload(1))
        assert ack.accepted
        await _wait_terminal(tm, ack.task_id)
    await asyncio.sleep(0.05)
    terminal = [r for r in tm._records.values() if r.state.is_terminal]
    assert len(terminal) <= 2
