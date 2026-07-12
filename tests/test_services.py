"""Service layer + loguru→LogEvent bridge (plan §6.1/§6.5, PR C-4).

The services are the only surface a frontend needs: commands in, catalog /
device / bus-health snapshots out, live events via subscription — exercised
here exactly the way the PyQt shell (and later the HTTP adapter) calls them."""
from __future__ import annotations

import asyncio
import threading
import uuid

import pytest
from loguru import logger

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.commands import AckCode
from phoebe.core.config import parse_app_config
from phoebe.core.events import RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

SLM_H, SLM_W = 60, 80


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
        },
    }


@pytest.fixture()
async def runtime(tmp_path):
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, runs_root=tmp_path / "runs", start_reaper=False)
    yield rt
    await rt.shutdown()


def _tpa_payload(steps: int = 2) -> dict:
    return {"max_steps": steps, "seed": 1,
            "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}


def _envelope(command: str, payload: dict) -> CommandEnvelope:
    return CommandEnvelope(command_id=f"cmd-{uuid.uuid4().hex[:8]}",
                           command=command, payload=payload)


async def test_run_service_submit_control_and_query(runtime):
    services = runtime.services
    ack = await services.runs.submit(_envelope("start_tpa_run",
                                               _tpa_payload(10_000)))
    assert ack.accepted and ack.code is AckCode.ACCEPTED
    await asyncio.sleep(0.05)
    assert ack.task_id in await services.runs.active_tasks()

    pause_ack = await services.runs.pause(ack.task_id)
    assert pause_ack.accepted
    cancel_ack = await services.runs.cancel(ack.task_id)
    assert cancel_ack.accepted
    assert (await runtime.task_manager.wait(ack.task_id)) is RunState.ABORTED
    await asyncio.sleep(0.05)

    runs = await services.runs.list_runs()
    assert len(runs) == 1
    assert runs[0].state == "aborted"
    journal = await services.runs.read_run_journal(runs[0].run_id)
    assert journal and journal[-1].record.value == "finalized"


async def test_device_service_table_and_stats(runtime):
    rows = await runtime.services.devices.table()
    assert {str(r.instrument_id) for r in rows} == {"slm.primary", "osa.main"}
    assert all(r.lifecycle == "ready" for r in rows)
    assert all(r.stats is not None for r in rows)

    stats = await runtime.services.devices.stats()
    assert set(stats) == {"slm.primary", "osa.main"}


async def test_event_service_snapshot_and_replay(runtime):
    services = runtime.services
    ack = await services.runs.submit(_envelope("start_tpa_run", _tpa_payload(2)))
    assert ack.accepted
    await runtime.task_manager.wait(ack.task_id)
    await asyncio.sleep(0.05)

    snapshot = await services.events.snapshot(["run_state", "device_health"])
    kinds = {e.event_type for e in snapshot}
    assert "run_state" in kinds and "device_health" in kinds
    run_states = [e for e in snapshot if e.event_type == "run_state"]
    assert run_states[-1].final                       # snapshot holds the final one

    seq_now = services.events.current_seq
    replayed = await services.events.replay_since(0, topics=["run_state"])
    assert replayed and replayed[-1].seq <= seq_now
    stats = await services.events.bus_stats()
    assert stats.current_seq == seq_now

    # subscription primed with the retained snapshot
    sub = await services.events.subscribe(["device_health"])
    first = await asyncio.wait_for(sub.get(), 2.0)
    assert first.event_type == "device_health"
    services.events.unsubscribe(sub)


async def test_plugin_service_schema_drives_forms(runtime):
    services = runtime.services
    commands = await services.plugins.commands()
    assert "start_tpa_run" in commands
    schema = await services.plugins.config_schema("start_tpa_run")
    assert schema is not None
    assert "max_steps" in schema["properties"]
    assert await services.plugins.config_schema("warp_drive") is None


async def test_service_hub_call_from_foreign_thread(runtime):
    """UI-thread pattern: ServiceHub.call returns a concurrent future."""
    results: list = []

    def qt_thread() -> None:
        future = runtime.services.call(runtime.services.devices.table())
        results.append(future.result(timeout=5))

    thread = threading.Thread(target=qt_thread)
    thread.start()
    await asyncio.get_running_loop().run_in_executor(None, thread.join)
    assert len(results[0]) == 2


# --------------------------------------------------------------- log bridge
async def test_plugin_logs_reach_the_bus(runtime):
    """Acceptance (C-4): ctx.log output is visible to frontends as LogEvents
    with task attribution."""
    sub = runtime.bus.subscribe(["log"], maxsize=1024)
    ack = await runtime.services.runs.submit(
        _envelope("start_tpa_run", _tpa_payload(2)))
    assert ack.accepted
    await runtime.task_manager.wait(ack.task_id)
    await asyncio.sleep(0.1)

    events = []
    while (ev := sub.get_nowait()) is not None:
        events.append(ev)
    ours = [e for e in events if str(e.task_id or "") == str(ack.task_id)]
    assert ours, "no LogEvent carried the run's task_id"


async def test_log_bridge_is_thread_safe(runtime):
    sub = runtime.bus.subscribe(["log"], maxsize=64)

    def worker() -> None:
        logger.bind(task_id="task_thread").info("hello from a worker thread")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    ev = await asyncio.wait_for(sub.get(), 2.0)
    while "hello from a worker thread" not in ev.message:
        ev = await asyncio.wait_for(sub.get(), 2.0)
    assert ev.level == "info"
    assert str(ev.task_id) == "task_thread"
