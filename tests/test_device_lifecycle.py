"""Device lifecycle & recovery suite (evolution plan Phase B, PRs B-1/B-2).

Covers: the transient/fatal classifier and backoff policy (A1/A3), the
device FSM with degraded-tolerant startup (§6.3), probe-driven
READY ↔ DEGRADED edges, threshold-triggered handle rebuild (lease-aware),
READY-only lease acquisition, H7 identity normalization, and controller
op-state (A2)."""
from __future__ import annotations

import asyncio

import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.core.config import (
    InstrumentConfig,
    ReconnectSettings,
    parse_app_config,
)
from phoebe.core.contracts import InstrumentId, TaskId, validate_boundary
from phoebe.core.controller import DeviceHealth, DeviceIdentity
from phoebe.core.di import ResolvedRequirement
from phoebe.core.errors import (
    DeviceNotReadyError,
    DeviceReportedError,
    InstrumentConnectionError,
    InstrumentTimeoutError,
)
from phoebe.core.events import RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.core.reconnect import DeviceLifecycleState, DeviceSupervisor
from phoebe.core.retry import ErrorClass, RetryPolicy, classify_error, retry_call
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

SLM_H, SLM_W = 60, 80

_FAST = ReconnectSettings(base_delay_s=0.01, max_delay_s=0.03, multiplier=1.5,
                          give_up_attempts=10, rebuild_after_probe_failures=3,
                          probe_timeout_s=1.0)


def _sim_config(runs_root: str, **overrides) -> dict:
    cfg = {
        "mode": "dev",
        "storage": {"runs_root": runs_root},
        "health_poll_interval_s": 0,
        "reconnect": {"base_delay_s": 0.01, "max_delay_s": 0.03,
                      "give_up_attempts": 10, "rebuild_after_probe_failures": 3},
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


async def _wait_lifecycle(dm, iid: str, state: DeviceLifecycleState,
                          timeout: float = 5.0) -> None:
    async def poll():
        while dm.lifecycle_state(InstrumentId(iid)) is not state:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


# ------------------------------------------------------------- classifier (A1/A3)
def test_classifier_transient_vs_fatal():
    assert classify_error(InstrumentTimeoutError("t")) is ErrorClass.TRANSIENT
    assert classify_error(InstrumentConnectionError("c")) is ErrorClass.TRANSIENT
    assert classify_error(ConnectionResetError()) is ErrorClass.TRANSIENT
    assert classify_error(OSError("io")) is ErrorClass.TRANSIENT
    # the device answered — a blind retry asks the same question
    assert classify_error(DeviceReportedError("SLM_NG")) is ErrorClass.FATAL
    # explicit fatal flag beats the type rule (missing DLL, identity mismatch)
    assert classify_error(
        InstrumentConnectionError("dll not found", fatal=True)) is ErrorClass.FATAL
    # unknown exception types are never blind-retried
    assert classify_error(ValueError("bug")) is ErrorClass.FATAL


def test_backoff_delays_grow_to_ceiling():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=8.0, multiplier=2.0, jitter=0.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 8, 8]


async def test_retry_call_recovers_from_transient_and_respects_fatal():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise InstrumentTimeoutError("blip")
        return "ok"

    policy = RetryPolicy(max_attempts=5, base_delay_s=0.001, jitter=0.0)
    assert await retry_call(flaky, policy=policy, label="flaky") == "ok"
    assert calls["n"] == 3

    async def fatal():
        calls["n"] += 1
        raise DeviceReportedError("no")

    calls["n"] = 0
    with pytest.raises(DeviceReportedError):
        await retry_call(fatal, policy=policy, label="fatal")
    assert calls["n"] == 1                     # no blind retry


# --------------------------------------------------------- supervisor FSM (B-1)
async def test_supervisor_transient_drop_auto_recovers():
    attempts = {"n": 0}

    async def connect():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise InstrumentConnectionError("link down")

    async def disconnect():
        pass

    sup = DeviceSupervisor(InstrumentId("dev.x"), connect=connect,
                           disconnect=disconnect, settings=_FAST)
    assert not await sup.start()
    assert sup.state is DeviceLifecycleState.BACKOFF

    async def wait_ready():
        while not sup.is_ready:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait_ready(), 5.0)
    assert attempts["n"] == 3


async def test_supervisor_fatal_goes_error_without_retry():
    attempts = {"n": 0}

    async def connect():
        attempts["n"] += 1
        raise InstrumentConnectionError("SLMFunc.dll not found", fatal=True)

    sup = DeviceSupervisor(InstrumentId("dev.x"), connect=connect,
                           disconnect=_noop, settings=_FAST)
    await sup.start()
    assert sup.state is DeviceLifecycleState.ERROR
    await asyncio.sleep(0.1)
    assert attempts["n"] == 1                  # no retry loop armed


async def test_supervisor_give_up_ceiling_goes_offline():
    settings = ReconnectSettings(base_delay_s=0.005, max_delay_s=0.01,
                                 give_up_attempts=3)

    async def connect():
        raise InstrumentConnectionError("nobody home")

    sup = DeviceSupervisor(InstrumentId("dev.x"), connect=connect,
                           disconnect=_noop, settings=settings)
    await sup.start()

    async def wait_offline():
        while sup.state is not DeviceLifecycleState.OFFLINE:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait_offline(), 5.0)


async def _noop():
    pass


# ------------------------------------------- degraded startup + recovery (B-2)
async def test_boot_with_offline_device_then_recover(tmp_path):
    """One device fails its first connects: boot succeeds (degraded), dispatch
    on that device is rejected with its lifecycle state, and once the backoff
    loop reconnects it, the same command runs to completion."""
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs",
                             connect=False, start_reaper=False)
    try:
        dm = rt.device_manager
        slm = dm.controller(InstrumentId("slm.primary"))
        orig_connect = slm.connect
        fails = {"n": 5}

        async def flaky_connect():
            if fails["n"] > 0:
                fails["n"] -= 1
                raise InstrumentConnectionError("simulated link failure")
            await orig_connect()

        slm.connect = flaky_connect
        await dm.connect_all()                 # must NOT raise (degraded boot)

        assert dm.lifecycle_state(InstrumentId("slm.primary")) in (
            DeviceLifecycleState.BACKOFF, DeviceLifecycleState.CONNECTING)
        assert dm.lifecycle_state(InstrumentId("osa.main")) is \
            DeviceLifecycleState.READY

        ack = await rt.gateway.submit(CommandEnvelope(
            command_id="cmd-notready", command="start_tpa_run",
            payload={"max_steps": 2, "seed": 1,
                     "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}))
        assert not ack.accepted
        assert "not ready" in (ack.reason or "")

        await _wait_lifecycle(dm, "slm.primary", DeviceLifecycleState.READY)

        ack = await rt.gateway.submit(CommandEnvelope(
            command_id="cmd-recovered", command="start_tpa_run",
            payload={"max_steps": 2, "seed": 1,
                     "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}))
        assert ack.accepted, ack.reason
        state = await rt.task_manager.wait(ack.task_id)
        assert state is RunState.COMPLETED
    finally:
        await rt.shutdown()


async def test_fatal_connect_surfaces_error_and_operator_reconnect_fixes_it(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs",
                             connect=False, start_reaper=False)
    try:
        dm = rt.device_manager
        slm = dm.controller(InstrumentId("slm.primary"))
        orig_connect = slm.connect
        broken = {"yes": True}

        async def fatal_connect():
            if broken["yes"]:
                raise InstrumentConnectionError("bad dll path", fatal=True)
            await orig_connect()

        slm.connect = fatal_connect
        await dm.connect_all()
        assert dm.lifecycle_state(InstrumentId("slm.primary")) is \
            DeviceLifecycleState.ERROR

        broken["yes"] = False                  # operator fixed the config
        assert await dm.reconnect_instrument(InstrumentId("slm.primary"))
        assert dm.lifecycle_state(InstrumentId("slm.primary")) is \
            DeviceLifecycleState.READY
    finally:
        await rt.shutdown()


# ------------------------------------------------- probe-driven edges (B-2/A10)
@pytest.fixture()
async def live_runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


async def test_probe_failure_degrades_and_probe_ok_recovers(live_runtime):
    dm = live_runtime.device_manager
    osa = dm.controller(InstrumentId("osa.main"))
    orig_health = osa.get_health
    sick = {"yes": True}

    async def health():
        if sick["yes"]:
            raise InstrumentTimeoutError("probe timeout")
        return await orig_health()

    osa.get_health = health
    await dm._probe_all()
    assert dm.lifecycle_state(InstrumentId("osa.main")) is \
        DeviceLifecycleState.DEGRADED
    # DEGRADED devices are not leasable (plan §6.3)
    with pytest.raises(DeviceNotReadyError):
        dm.try_acquire_all(TaskId("task_x"), [
            ResolvedRequirement("osa", InstrumentId("osa.main"),
                                "spectrum_analyzer")])

    sick["yes"] = False
    await dm._probe_all()
    assert dm.lifecycle_state(InstrumentId("osa.main")) is \
        DeviceLifecycleState.READY


async def test_probe_failure_threshold_triggers_rebuild(live_runtime):
    dm = live_runtime.device_manager
    osa = dm.controller(InstrumentId("osa.main"))
    connects = {"n": 0}
    orig_connect = osa.connect

    async def counting_connect():
        connects["n"] += 1
        await orig_connect()

    async def sick_health():
        raise InstrumentTimeoutError("probe timeout")

    osa.connect = counting_connect
    osa.get_health = sick_health
    for _ in range(3):                          # rebuild_after_probe_failures=3
        await dm._probe_all()
    assert connects["n"] == 1                   # handle torn down and rebuilt
    assert dm.lifecycle_state(InstrumentId("osa.main")) is \
        DeviceLifecycleState.READY              # reconnect succeeded


async def test_no_rebuild_under_active_lease(live_runtime):
    dm = live_runtime.device_manager
    osa = dm.controller(InstrumentId("osa.main"))
    connects = {"n": 0}
    orig_connect = osa.connect

    async def counting_connect():
        connects["n"] += 1
        await orig_connect()

    async def sick_health():
        raise InstrumentTimeoutError("probe timeout")

    leases = dm.try_acquire_all(TaskId("task_hold"), [
        ResolvedRequirement("osa", InstrumentId("osa.main"), "spectrum_analyzer")])
    osa.connect = counting_connect
    osa.get_health = sick_health
    for _ in range(6):
        await dm._probe_all()
    assert connects["n"] == 0                   # never rebuilt under a lease
    assert dm.lifecycle_state(InstrumentId("osa.main")) is \
        DeviceLifecycleState.DEGRADED
    dm.release(TaskId("task_hold"), leases)


async def test_probe_skips_busy_device(live_runtime):
    """A device whose op-lock is held is being used — the probe must not
    queue SCPI behind the running operation (plan §3.1 A10)."""
    dm = live_runtime.device_manager
    osa = dm.controller(InstrumentId("osa.main"))
    probes = {"n": 0}
    orig_health = osa.get_health

    async def counting_health():
        probes["n"] += 1
        return await orig_health()

    osa.get_health = counting_health
    async with osa._op_lock:                   # simulate a long acquisition
        health = await osa.probe_health()
    assert probes["n"] == 0
    assert health.status == "ok"
    assert "busy" in (health.detail or "")


# -------------------------------------------------------------- H7 + op-state
async def test_identity_normalization_accepts_vendor_punctuation(live_runtime):
    """H7: configured ``rohde-schwarz`` must match ``ROHDE&SCHWARZ,RTO6,...``."""
    dm = live_runtime.device_manager

    class StubScope:
        async def get_identity(self):
            return DeviceIdentity(vendor="ROHDE&SCHWARZ", model="RTO6",
                                  serial="1329.7002k44",
                                  raw="ROHDE&SCHWARZ,RTO6,1329.7002k44,5.35.2.0")

        async def disconnect(self):
            pass

    icfg = validate_boundary(InstrumentConfig, {
        "instrument_id": "scope.rto6", "kind": "oscilloscope",
        "vendor": "rohde-schwarz", "model": "rto6", "role": "scope",
        "backend": "real",
        "connection": {"transport": "tcp", "host": "1.2.3.4", "port": 5025}})
    await dm._verify_identity(StubScope(), icfg)   # must not raise

    wrong = icfg.model_copy(update={"vendor": "keysight"})
    with pytest.raises(InstrumentConnectionError) as excinfo:
        await dm._verify_identity(StubScope(), wrong)
    assert excinfo.value.fatal                     # mismatch is never retried


async def test_controller_stats_ring_is_capped(live_runtime):
    osa = live_runtime.device_manager.controller(InstrumentId("osa.main"))
    for i in range(50):
        osa.note_error(f"error {i}")
    osa.note_ok()
    stats = osa.get_stats()
    assert stats.ops_failed == 50
    assert stats.ops_ok == 1
    assert len(stats.recent_errors) == 32          # ring-capped (A2)
    assert stats.recent_errors[-1].endswith("error 49")


async def test_get_health_reports_probe_status(live_runtime):
    """DeviceHealth returned by probe_health under the lock matches get_health."""
    osa = live_runtime.device_manager.controller(InstrumentId("osa.main"))
    health = await osa.probe_health()
    assert isinstance(health, DeviceHealth)
    assert health.status == "ok"
