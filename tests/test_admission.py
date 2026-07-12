"""Typed admission chain + CommandLedger (plan §6.4, PR C-3).

Acceptance: duplicate / conflict / restart admission behaviour, and every
``AckCode`` produced by a unit test — clients branch on codes, so each code
path must be provably reachable."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.commands import AckCode
from phoebe.core.command_ledger import CommandLedger
from phoebe.core.config import parse_app_config
from phoebe.core.contracts import ContractModel, InstrumentId
from phoebe.core.di import Depends
from phoebe.core.events import RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.core.plugin import Plugin, PluginRegistry, on_command, register
from phoebe.core.reconnect import DeviceLifecycleState
from phoebe.instruments.protocols import PatternModulator
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

SLM_H, SLM_W = 60, 80


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
        "plugins": {
            "org.lab.tpa_multiplier": {"bindings": {"slm": "primary_slm",
                                                    "osa": "main_osa"}},
            "org.lab.spectrum_grid": {"bindings": {"slm": "primary_slm",
                                                   "osa": "main_osa"}},
        },
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture()
async def runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


def _tpa_payload(steps: int = 2) -> dict:
    return {"max_steps": steps, "seed": 1,
            "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}


def _envelope(command: str, payload: dict, *, command_id: str | None = None):
    return CommandEnvelope(command_id=command_id or f"cmd-{uuid.uuid4().hex[:8]}",
                           command=command, payload=payload)


# ------------------------------------------------------- chain rejections
async def test_unknown_command_code(runtime):
    ack = await runtime.gateway.submit(_envelope("warp_drive", {}))
    assert not ack.accepted and ack.code is AckCode.UNKNOWN_COMMAND


async def test_invalid_payload_code(runtime):
    ack = await runtime.gateway.submit(
        _envelope("start_tpa_run", {"max_steps": "not-an-int"}))
    assert not ack.accepted and ack.code is AckCode.INVALID_PAYLOAD
    assert "invalid payload" in (ack.reason or "")


async def test_maintenance_mode_code(runtime):
    runtime.task_manager.enter_maintenance("scheduled service")
    ack = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload()))
    assert not ack.accepted and ack.code is AckCode.MAINTENANCE_MODE
    runtime.task_manager.exit_maintenance()
    ack = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload()))
    assert ack.accepted and ack.code is AckCode.ACCEPTED
    runtime.task_manager.request_cancel(ack.task_id)
    await runtime.task_manager.wait(ack.task_id)


async def test_device_busy_code_under_reject_policy(runtime):
    ack1 = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(10_000)))
    assert ack1.accepted
    await asyncio.sleep(0.05)
    ack2 = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload()))
    assert not ack2.accepted and ack2.code is AckCode.DEVICE_BUSY
    assert "locked by" in (ack2.reason or "")
    runtime.task_manager.request_cancel(ack1.task_id)
    await runtime.task_manager.wait(ack1.task_id)


async def test_queued_code_under_queue_policy(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs"),
                                       dispatch_policy="queue"))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    try:
        ack1 = await rt.gateway.submit(
            _envelope("start_tpa_run", _tpa_payload(10_000)))
        assert ack1.code is AckCode.ACCEPTED
        ack2 = await rt.gateway.submit(
            _envelope("start_tpa_run", _tpa_payload()))
        assert ack2.accepted and ack2.queued and ack2.code is AckCode.QUEUED
        rt.task_manager.request_cancel(ack2.task_id)
        rt.task_manager.request_cancel(ack1.task_id)
        await rt.task_manager.wait(ack1.task_id)
    finally:
        await rt.shutdown()


async def test_device_not_ready_code(runtime):
    supervisor = runtime.device_manager.supervisor(InstrumentId("slm.primary"))
    assert supervisor is not None
    await supervisor.disable()
    assert supervisor.state is DeviceLifecycleState.OFFLINE
    ack = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload()))
    assert not ack.accepted and ack.code is AckCode.DEVICE_NOT_READY
    assert ack.error is not None
    assert str(ack.error.instrument_id) == "slm.primary"


async def test_health_stale_code(runtime, monkeypatch):
    """READY but unconfirmed for too long → HEALTH_STALE (poller down)."""
    monkeypatch.setattr(runtime.task_manager._admission,
                        "_health_stale_after_s", 10.0)
    supervisor = runtime.device_manager.supervisor(InstrumentId("osa.main"))
    assert supervisor is not None and supervisor.is_ready
    supervisor._last_confirm_mono -= 3600.0        # confirmed an hour ago
    ack = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload()))
    assert not ack.accepted and ack.code is AckCode.HEALTH_STALE
    assert ack.error is not None
    assert str(ack.error.instrument_id) == "osa.main"


async def test_missing_role_and_kind_mismatch_codes(tmp_path):
    registry = PluginRegistry()

    class RoleConfig(ContractModel):
        pass

    @register(plugin_id="test.wants_role", registry=registry)
    class WantsRole(Plugin):
        config_type = RoleConfig

        @on_command("start_wants_role")
        async def run(self, config, ctx,
                      slm: PatternModulator = Depends(role="no_such_role")) -> None:
            pass

    @register(plugin_id="test.wants_unknown_kind", registry=registry)
    class WantsUnknownKind(Plugin):
        config_type = RoleConfig

        @on_command("start_wants_unknown_kind")
        async def run(self, config, ctx,
                      dev: object = Depends()) -> None:   # not a protocol
            pass

    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, plugins=registry, runs_root=tmp_path / "runs",
                             start_reaper=False)
    try:
        ack = await rt.gateway.submit(_envelope("start_wants_role", {}))
        assert not ack.accepted and ack.code is AckCode.MISSING_ROLE
        ack = await rt.gateway.submit(_envelope("start_wants_unknown_kind", {}))
        assert not ack.accepted and ack.code is AckCode.KIND_MISMATCH
    finally:
        await rt.shutdown()


async def test_plugin_api_incompatible_code(tmp_path):
    registry = PluginRegistry()

    class FutureConfig(ContractModel):
        pass

    @register(plugin_id="test.from_the_future", registry=registry)
    class FuturePlugin(Plugin):
        config_type = FutureConfig
        api_version = 999

        @on_command("start_future_run")
        async def run(self, config, ctx,
                      slm: PatternModulator = Depends(role="primary_slm")) -> None:
            pass

    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, plugins=registry, runs_root=tmp_path / "runs",
                             start_reaper=False)
    try:
        ack = await rt.gateway.submit(_envelope("start_future_run", {}))
        assert not ack.accepted
        assert ack.code is AckCode.PLUGIN_API_INCOMPATIBLE
    finally:
        await rt.shutdown()


# ------------------------------------------------------- builtin ack codes
async def test_builtin_codes(runtime):
    ack = await runtime.gateway.submit(_envelope("pause", {}))
    assert ack.code is AckCode.INVALID_PAYLOAD
    ack = await runtime.gateway.submit(_envelope("pause", {"task_id": "task_nope"}))
    assert ack.code is AckCode.UNKNOWN_TASK
    started = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(10_000)))
    assert started.accepted
    await asyncio.sleep(0.05)
    ack = await runtime.gateway.submit(                      # resume while RUNNING
        _envelope("resume", {"task_id": str(started.task_id)}))
    assert ack.code is AckCode.INVALID_STATE
    ack = await runtime.gateway.submit(
        _envelope("cancel", {"task_id": str(started.task_id)}))
    assert ack.accepted and ack.code is AckCode.ACCEPTED
    await runtime.task_manager.wait(started.task_id)


# --------------------------------------------------- ledger: idempotency
async def test_duplicate_command_is_replayed_not_restarted(runtime):
    envelope = _envelope("start_tpa_run", _tpa_payload(2))
    first = await runtime.gateway.submit(envelope)
    assert first.accepted and first.code is AckCode.ACCEPTED
    await runtime.task_manager.wait(first.task_id)

    replay = await runtime.gateway.submit(envelope)          # same id + payload
    assert replay.accepted and replay.code is AckCode.REPLAYED
    assert replay.task_id == first.task_id                   # the ORIGINAL run
    # no second run was created
    assert runtime.catalog is not None
    assert len(runtime.catalog.list_runs()) == 1


async def test_same_id_different_payload_conflicts(runtime):
    command_id = f"cmd-{uuid.uuid4().hex[:8]}"
    first = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(2), command_id=command_id))
    assert first.accepted
    await runtime.task_manager.wait(first.task_id)

    conflict = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(3), command_id=command_id))
    assert not conflict.accepted
    assert conflict.code is AckCode.COMMAND_ID_CONFLICT


async def test_rejected_commands_are_not_recorded(runtime):
    """A rejection holds nothing — retrying the same id after fixing the
    problem must be legal."""
    command_id = f"cmd-{uuid.uuid4().hex[:8]}"
    runtime.task_manager.enter_maintenance("brb")
    rejected = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(2), command_id=command_id))
    assert rejected.code is AckCode.MAINTENANCE_MODE
    runtime.task_manager.exit_maintenance()
    retry = await runtime.gateway.submit(
        _envelope("start_tpa_run", _tpa_payload(2), command_id=command_id))
    assert retry.accepted and retry.code is AckCode.ACCEPTED
    await runtime.task_manager.wait(retry.task_id)


def test_ledger_survives_restart(tmp_path):
    """Same sqlite file, new process → the first ack is still replayed."""
    path = tmp_path / "ledger.sqlite3"
    envelope = _envelope("start_tpa_run", _tpa_payload(2), command_id="cmd-boot")
    from phoebe.contracts.commands import CommandAck
    from phoebe.core.contracts import TaskId

    ledger = CommandLedger(path)
    assert ledger.check(envelope) is None
    ledger.record(envelope, CommandAck(command_id="cmd-boot", accepted=True,
                                       code=AckCode.ACCEPTED,
                                       task_id=TaskId("task_orig")))
    ledger.close()

    reopened = CommandLedger(path)                           # "after restart"
    verdict = reopened.check(envelope)
    assert verdict is not None and verdict.code is AckCode.REPLAYED
    assert verdict.task_id == "task_orig"
    conflicting = _envelope("start_tpa_run", _tpa_payload(9),
                            command_id="cmd-boot")
    verdict = reopened.check(conflicting)
    assert verdict is not None and verdict.code is AckCode.COMMAND_ID_CONFLICT
    reopened.close()


async def test_replayed_while_still_running_never_double_starts(runtime):
    envelope = _envelope("start_tpa_run", _tpa_payload(10_000))
    first = await runtime.gateway.submit(envelope)
    assert first.accepted
    await asyncio.sleep(0.05)
    replay = await runtime.gateway.submit(envelope)
    assert replay.code is AckCode.REPLAYED
    assert replay.task_id == first.task_id
    assert len(runtime.task_manager.active_tasks()) == 1     # one run, not two
    runtime.task_manager.request_cancel(first.task_id)
    assert (await runtime.task_manager.wait(first.task_id)) is RunState.ABORTED
