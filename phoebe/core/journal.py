"""RunJournal: append-only per-run lifecycle facts + startup recovery scan
(evolution plan §6.2, PR C-2).

The journal is the persisted truth about a run, on two independent axes:
``execution_outcome`` (what the plugin did) and ``finalized(ok|degraded)``
(whether cleanup completed).  Every record is one JSON line in
``<run_dir>/journal.jsonl``, flushed immediately; the crash-relevant records
(EXECUTION_OUTCOME, FINALIZED) are additionally fsynced.

A journal that ends without FINALIZED is an interrupted run.  The startup
recovery scan explains it from files alone — **zero device I/O**: sim runs
are marked ``interrupted``; anything that touched real hardware is marked
``operator_review_required`` because no code can know what state the bench
was left in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..contracts.base import RunId, TaskId, timestamps, validate_boundary
from ..contracts.run import (
    ExecutionOutcome,
    FinalizedResult,
    JournalRecordType,
    RecoveryReport,
    RecoveryResolution,
    RunJournalRecord,
)

if TYPE_CHECKING:
    from .catalog import RunCatalog

JOURNAL_FILENAME = "journal.jsonl"

#: Records after which a crash is unrecoverable ambiguity → fsync.
_FSYNC_RECORDS = frozenset({
    JournalRecordType.EXECUTION_OUTCOME,
    JournalRecordType.FINALIZED,
})

#: What the operator should conclude from the last record a crashed run
#: managed to journal ("4-point kill tests explained after restart").
_EXPLANATIONS: dict[JournalRecordType, str] = {
    JournalRecordType.ADMITTED:
        "process died during run preparation (before the run directory was "
        "complete); no experiment data was produced",
    JournalRecordType.RUN_DIR_CREATED:
        "process died during run preparation (run directory created, devices "
        "not yet staged); no experiment data was produced",
    JournalRecordType.BASELINE_CAPTURED:
        "process died during run preparation (baseline captured, devices not "
        "yet staged); no experiment data was produced",
    JournalRecordType.STAGED:
        "process died after device staging but before execution started; "
        "devices may not have been unstaged",
    JournalRecordType.EXECUTION_STARTED:
        "process died while the experiment was executing; data files are "
        "partial and devices were not cleaned up",
    JournalRecordType.EXECUTION_OUTCOME:
        "the experiment finished but the process died during cleanup; data "
        "is complete but devices/writer may not have been released",
    JournalRecordType.CLEANUP_STARTED:
        "process died mid-cleanup; the writer may hold unflushed data and "
        "devices may not have been released",
    JournalRecordType.WRITER_CLOSED:
        "process died after the writer closed but before leases were "
        "released; data is complete",
    JournalRecordType.LEASES_RELEASED:
        "process died at the very end of cleanup (only the finalized record "
        "is missing); data is complete",
}


class RunJournal:
    """Single-run journal writer.  Never raises into the run: a journal I/O
    failure is logged and the journal goes inert — the run must not die
    because its bookkeeping did."""

    def __init__(self, run_dir: Path, *, task_id: TaskId, run_id: RunId,
                 catalog: RunCatalog | None = None) -> None:
        self._path = run_dir / JOURNAL_FILENAME
        self._task_id = task_id
        self._run_id = run_id
        self._catalog = catalog
        self._file = None
        self._broken = False

    def record(self, record_type: JournalRecordType, *,
               outcome: ExecutionOutcome | None = None,
               finalized: FinalizedResult | None = None,
               resolution: RecoveryResolution | None = None,
               detail: str | None = None) -> None:
        entry = RunJournalRecord(
            record=record_type, task_id=self._task_id, run_id=self._run_id,
            outcome=outcome, finalized=finalized, resolution=resolution,
            detail=detail, **timestamps(),
        )
        if not self._broken:
            try:
                if self._file is None:
                    self._file = self._path.open("a", encoding="utf-8", newline="\n")
                self._file.write(entry.model_dump_json() + "\n")
                self._file.flush()
                if record_type in _FSYNC_RECORDS:
                    os.fsync(self._file.fileno())
            except OSError:
                self._broken = True
                logger.exception("run journal {} failed; journaling disabled "
                                 "for this run", self._path)
        if self._catalog is not None:
            try:
                self._catalog.apply_record(entry)
            except Exception:
                logger.exception("catalog update for {} failed", self._run_id)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                logger.exception("run journal {} close failed", self._path)
            self._file = None


def read_journal(run_dir: Path) -> list[RunJournalRecord]:
    """Parse a run directory's journal; skips corrupt lines (a crash can
    truncate the final line mid-write)."""
    path = run_dir / JOURNAL_FILENAME
    records: list[RunJournalRecord] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(RunJournalRecord.model_validate_json(line))
        except Exception:
            logger.warning("skipping corrupt journal line in {}", path)
    return records


def _is_sim_run(run_dir: Path) -> bool:
    """True when the manifest proves every instrument ran backend='sim'.
    Unknown/missing manifest → treated as real (fail-closed)."""
    manifest_path = run_dir / "run.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        instruments = raw.get("instruments", {})
        backends = [spec.get("backend") for spec in instruments.values()]
        return bool(backends) and all(b == "sim" for b in backends)
    except (OSError, ValueError, AttributeError):
        return False


def scan_and_recover(runs_root: Path, *,
                     catalog: RunCatalog | None = None) -> list[RecoveryReport]:
    """Startup recovery scan (plan §6.2): explain every journal that ends
    without FINALIZED, append a RECOVERED record, and update the catalog.

    Reads and writes **files only** — never touches a device, a transport or
    a controller; safe to run before any hardware is connected.
    """
    reports: list[RecoveryReport] = []
    if not runs_root.exists():
        return reports
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        records = read_journal(run_dir)
        if not records:
            continue                                   # pre-journal or foreign dir
        types = {r.record for r in records}
        if JournalRecordType.FINALIZED in types or JournalRecordType.RECOVERED in types:
            continue                                   # complete or already explained
        last = records[-1]
        resolution: RecoveryResolution = (
            "interrupted" if _is_sim_run(run_dir) else "operator_review_required"
        )
        explanation = _EXPLANATIONS.get(
            last.record, "journal ended in an unexpected state")
        report = validate_boundary(RecoveryReport, {
            "run_id": str(last.run_id), "task_id": str(last.task_id),
            "run_dir": run_dir.name, "last_record": last.record.value,
            "resolution": resolution, "explanation": explanation,
        })
        reports.append(report)
        recovery_journal = RunJournal(run_dir, task_id=last.task_id,
                                      run_id=last.run_id, catalog=catalog)
        try:
            recovery_journal.record(JournalRecordType.RECOVERED,
                                    resolution=resolution, detail=explanation)
        finally:
            recovery_journal.close()
        logger.warning("recovered run {}: {} ({})", last.run_id, resolution,
                       explanation)
    return reports
