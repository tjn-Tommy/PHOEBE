"""RunJournal + recovery scan + run catalog (plan §6.2, PR C-2).

The journal is the persisted truth: a full sim run writes the complete record
sequence on both axes (execution outcome / finalization); a crash at any of
the four kill points leaves a journal the startup scan can explain — from
files alone, with zero device I/O."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.run import JournalRecordType
from phoebe.core.catalog import RunCatalog
from phoebe.core.config import parse_app_config
from phoebe.core.contracts import RunId, TaskId
from phoebe.core.events import RunState
from phoebe.core.gateway import CommandEnvelope
from phoebe.core.journal import RunJournal, read_journal, scan_and_recover
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


async def _submit(rt, command: str, payload: dict):
    return await rt.gateway.submit(CommandEnvelope(
        command_id=f"cmd-{uuid.uuid4().hex[:8]}", command=command,
        payload=payload))


def _tpa_payload(steps: int) -> dict:
    return {"max_steps": steps, "seed": 1,
            "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}}


def _run_dirs(runs_root):
    return [p for p in runs_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")]


# ------------------------------------------------------------- live journal
async def test_completed_run_writes_full_journal(runtime, tmp_path):
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(3))
    assert ack.accepted, ack.reason
    assert (await runtime.task_manager.wait(ack.task_id)) is RunState.COMPLETED
    await asyncio.sleep(0.05)

    run_dir = _run_dirs(tmp_path / "runs")[0]
    records = read_journal(run_dir)
    sequence = [r.record for r in records]
    assert sequence == [
        JournalRecordType.ADMITTED,
        JournalRecordType.RUN_DIR_CREATED,
        JournalRecordType.BASELINE_CAPTURED,
        JournalRecordType.STAGED,
        JournalRecordType.EXECUTION_STARTED,
        JournalRecordType.EXECUTION_OUTCOME,
        JournalRecordType.CLEANUP_STARTED,
        JournalRecordType.WRITER_CLOSED,
        JournalRecordType.LEASES_RELEASED,
        JournalRecordType.FINALIZED,
    ]
    outcome = next(r for r in records
                   if r.record is JournalRecordType.EXECUTION_OUTCOME)
    final = records[-1]
    assert outcome.outcome == "completed"
    assert final.finalized == "ok"

    # catalog row projected from the same records
    row = runtime.catalog.get(records[0].run_id)
    assert row is not None
    assert row.state == "completed"
    assert row.execution_outcome == "completed"
    assert row.finalized == "ok"
    assert row.plugin_id == "org.lab.tpa_multiplier"


async def test_aborted_run_records_outcome_aborted_finalized_ok(runtime, tmp_path):
    """The two axes are independent: a user cancel with clean cleanup is
    outcome=aborted, finalized=ok (plan §6.2)."""
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(10_000))
    assert ack.accepted
    await asyncio.sleep(0.1)
    runtime.task_manager.request_cancel(ack.task_id)
    assert (await runtime.task_manager.wait(ack.task_id)) is RunState.ABORTED
    await asyncio.sleep(0.05)

    run_dir = _run_dirs(tmp_path / "runs")[0]
    records = read_journal(run_dir)
    outcome = next(r for r in records
                   if r.record is JournalRecordType.EXECUTION_OUTCOME)
    assert outcome.outcome == "aborted"
    assert records[-1].record is JournalRecordType.FINALIZED
    assert records[-1].finalized == "ok"


async def test_degraded_cleanup_is_persisted(runtime, tmp_path):
    """A cleanup failure must be visible forever: finalized=degraded."""
    slm = runtime.device_manager.controller("slm.primary")

    async def broken_unstage():
        raise RuntimeError("unstage exploded")

    slm.unstage = broken_unstage
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(2))
    assert ack.accepted
    await runtime.task_manager.wait(ack.task_id)
    await asyncio.sleep(0.05)

    run_dir = _run_dirs(tmp_path / "runs")[0]
    final = read_journal(run_dir)[-1]
    assert final.record is JournalRecordType.FINALIZED
    assert final.finalized == "degraded"
    row = runtime.catalog.get(final.run_id)
    assert row is not None and row.finalized == "degraded"


# ---------------------------------------------------------- recovery scan
def _fake_run_dir(runs_root, name: str, *, backend: str,
                  cut_after: JournalRecordType) -> RunId:
    """Synthesize a crashed run: manifest + journal truncated at a kill point."""
    run_dir = runs_root / name
    run_dir.mkdir(parents=True)
    run_id = RunId(f"run_{name}")
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": str(run_id), "task_id": "task_x",
        "plugin_id": "org.lab.tpa_multiplier", "command": "start_tpa_run",
        "created_at": "2026-07-12T10:00:00+00:00",
        "instruments": {"slm.primary": {"backend": backend},
                        "osa.main": {"backend": backend}},
    }), encoding="utf-8")
    order = [
        JournalRecordType.ADMITTED,
        JournalRecordType.RUN_DIR_CREATED,
        JournalRecordType.BASELINE_CAPTURED,
        JournalRecordType.STAGED,
        JournalRecordType.EXECUTION_STARTED,
        JournalRecordType.EXECUTION_OUTCOME,
        JournalRecordType.CLEANUP_STARTED,
        JournalRecordType.WRITER_CLOSED,
        JournalRecordType.LEASES_RELEASED,
    ]
    journal = RunJournal(run_dir, task_id=TaskId("task_x"), run_id=run_id)
    for record_type in order:
        journal.record(record_type,
                       outcome=("completed" if record_type
                                is JournalRecordType.EXECUTION_OUTCOME else None))
        if record_type is cut_after:
            break
    journal.close()
    return run_id


def test_four_point_kill_recovery_explains_each_crash(tmp_path):
    """Plan C-2 acceptance: kill points at PREPARING / RUNNING / mid-cleanup /
    pre-FINALIZED are each explained after restart."""
    runs_root = tmp_path / "runs"
    kill_points = {
        "kill1_preparing": JournalRecordType.RUN_DIR_CREATED,
        "kill2_running": JournalRecordType.EXECUTION_STARTED,
        "kill3_cleanup": JournalRecordType.EXECUTION_OUTCOME,
        "kill4_almost_done": JournalRecordType.LEASES_RELEASED,
    }
    for name, cut in kill_points.items():
        _fake_run_dir(runs_root, name, backend="sim", cut_after=cut)

    catalog = RunCatalog(runs_root / ".phoebe" / "catalog.sqlite3")
    reports = scan_and_recover(runs_root, catalog=catalog)
    assert len(reports) == 4
    by_dir = {r.run_dir: r for r in reports}

    assert "preparation" in by_dir["kill1_preparing"].explanation
    assert "executing" in by_dir["kill2_running"].explanation
    assert "died during cleanup" in by_dir["kill3_cleanup"].explanation
    assert "only the finalized record is missing" in by_dir["kill4_almost_done"].explanation
    assert all(r.resolution == "interrupted" for r in reports)   # sim runs

    # the scan appended a RECOVERED record → a second scan is a no-op
    for name in kill_points:
        assert read_journal(runs_root / name)[-1].record is JournalRecordType.RECOVERED
    assert scan_and_recover(runs_root, catalog=catalog) == []
    catalog.close()


def test_real_run_recovery_requires_operator_review(tmp_path):
    """A crashed run that touched real hardware is never auto-closed — and the
    scan performs zero device I/O (nothing here could even open a device)."""
    runs_root = tmp_path / "runs"
    _fake_run_dir(runs_root, "real_crash", backend="real",
                  cut_after=JournalRecordType.EXECUTION_STARTED)
    reports = scan_and_recover(runs_root)
    assert len(reports) == 1
    assert reports[0].resolution == "operator_review_required"
    assert reports[0].last_record is JournalRecordType.EXECUTION_STARTED


async def test_startup_recovery_via_build_runtime(tmp_path):
    """An interrupted journal in runs_root is explained during bootstrap and
    lands in the catalog."""
    runs_root = tmp_path / "runs"
    run_id = _fake_run_dir(runs_root, "crashed_before_boot", backend="sim",
                           cut_after=JournalRecordType.EXECUTION_STARTED)
    cfg = parse_app_config(_sim_config(str(runs_root)))
    rt = await build_runtime(cfg, runs_root=runs_root, start_reaper=False)
    try:
        row = rt.catalog.get(run_id)
        assert row is not None
        assert row.state == "interrupted"
    finally:
        await rt.shutdown()


# ----------------------------------------------------------------- catalog
async def test_catalog_rebuild_matches_live_index(runtime, tmp_path):
    ack = await _submit(runtime, "start_tpa_run", _tpa_payload(2))
    assert ack.accepted
    await runtime.task_manager.wait(ack.task_id)
    await asyncio.sleep(0.05)

    live = runtime.catalog.list_runs()
    assert len(live) == 1

    rebuilt = RunCatalog(":memory:")
    count = rebuilt.rebuild(tmp_path / "runs")
    assert count == 1
    rows = rebuilt.list_runs()
    assert rows == live
    rebuilt.close()
