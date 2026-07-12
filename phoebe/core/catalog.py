"""Run catalog: queryable SQLite index over run directories (plan §6.2 /
B10, PR C-2).

The catalog is a *derived* index — the per-run journal + manifest files stay
the source of truth, so the index can always be rebuilt from the run dirs
(``rebuild()``).  Writes happen only at run-lifecycle boundaries (a handful
per run), so the small synchronous sqlite calls are fine on the loop thread.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from loguru import logger

from ..contracts.base import RunId, validate_boundary
from ..contracts.run import JournalRecordType, RunJournalRecord, RunResult

CATALOG_FILENAME = "catalog.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL DEFAULT '',
    plugin_id         TEXT NOT NULL DEFAULT '',
    command           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT '',
    run_dir           TEXT NOT NULL DEFAULT '',
    state             TEXT NOT NULL DEFAULT '',
    execution_outcome TEXT,
    finalized         TEXT,
    updated_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_created_at ON runs(created_at);
"""

#: Interactive state implied by each journal record (the catalog is a
#: projection of the journal — one mapping serves live updates AND rebuild).
_STATE_OF_RECORD: dict[JournalRecordType, str] = {
    JournalRecordType.ADMITTED: "preparing",
    JournalRecordType.RUN_DIR_CREATED: "preparing",
    JournalRecordType.BASELINE_CAPTURED: "preparing",
    JournalRecordType.STAGED: "preparing",
    JournalRecordType.EXECUTION_STARTED: "running",
    JournalRecordType.EXECUTION_OUTCOME: "finalizing",
    JournalRecordType.CLEANUP_STARTED: "finalizing",
    JournalRecordType.WRITER_CLOSED: "finalizing",
    JournalRecordType.LEASES_RELEASED: "finalizing",
}


class RunCatalog:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------- writes
    def register_run(self, *, run_id: str, task_id: str, plugin_id: str,
                     command: str, created_at: str, run_dir: str) -> None:
        """Create/refresh the descriptive part of a row (run-dir creation
        time); journal records then advance its state columns."""
        self._db.execute(
            """INSERT INTO runs(run_id, task_id, plugin_id, command, created_at,
                                run_dir, state, updated_at)
               VALUES(?,?,?,?,?,?, 'preparing', ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 task_id=excluded.task_id, plugin_id=excluded.plugin_id,
                 command=excluded.command, created_at=excluded.created_at,
                 run_dir=excluded.run_dir, updated_at=excluded.updated_at""",
            (run_id, task_id, plugin_id, command, created_at, run_dir, created_at),
        )
        self._db.commit()

    def apply_record(self, entry: RunJournalRecord) -> None:
        """Project one journal record onto the row (used by live journaling,
        the recovery scan and rebuild — a single code path)."""
        run_id = str(entry.run_id)
        updated = entry.t_wall.isoformat()
        self._db.execute(
            """INSERT INTO runs(run_id, task_id, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at""",
            (run_id, str(entry.task_id), updated),
        )
        if entry.record is JournalRecordType.EXECUTION_OUTCOME:
            self._db.execute(
                "UPDATE runs SET execution_outcome=?, state='finalizing' WHERE run_id=?",
                (entry.outcome, run_id),
            )
        elif entry.record is JournalRecordType.FINALIZED:
            self._db.execute(
                """UPDATE runs SET finalized=?,
                          state=COALESCE(execution_outcome, 'finalizing')
                   WHERE run_id=?""",
                (entry.finalized, run_id),
            )
        elif entry.record is JournalRecordType.RECOVERED:
            self._db.execute(
                "UPDATE runs SET state=? WHERE run_id=?",
                (entry.resolution or "interrupted", run_id),
            )
        else:
            state = _STATE_OF_RECORD.get(entry.record)
            if state is not None:
                self._db.execute(
                    "UPDATE runs SET state=? WHERE run_id=?", (state, run_id))
        self._db.commit()

    # ------------------------------------------------------------- queries
    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[RunResult]:
        rows = self._db.execute(
            """SELECT run_id, task_id, plugin_id, command, created_at, run_dir,
                      state, execution_outcome, finalized
               FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [self._to_result(row) for row in rows]

    def get(self, run_id: RunId | str) -> RunResult | None:
        row = self._db.execute(
            """SELECT run_id, task_id, plugin_id, command, created_at, run_dir,
                      state, execution_outcome, finalized
               FROM runs WHERE run_id=?""",
            (str(run_id),),
        ).fetchone()
        return self._to_result(row) if row is not None else None

    @staticmethod
    def _to_result(row: tuple) -> RunResult:
        (run_id, task_id, plugin_id, command, created_at, run_dir,
         state, outcome, finalized) = row
        return validate_boundary(RunResult, {
            "run_id": run_id, "task_id": task_id, "plugin_id": plugin_id,
            "command": command,
            "created_at": created_at or "1970-01-01T00:00:00+00:00",
            "run_dir": run_dir, "state": state,
            "execution_outcome": outcome, "finalized": finalized,
        })

    # ------------------------------------------------------------- rebuild
    def rebuild(self, runs_root: Path) -> int:
        """Drop the index and repopulate it from the run directories (the
        journals + manifests are the truth; the catalog is disposable).
        Returns the number of indexed runs."""
        from .journal import read_journal                 # local: avoid cycle

        self._db.execute("DELETE FROM runs")
        self._db.commit()
        count = 0
        if not runs_root.exists():
            return count
        for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
            records = read_journal(run_dir)
            if not records:
                continue
            manifest = self._read_manifest(run_dir)
            self.register_run(
                run_id=str(records[0].run_id),
                task_id=str(records[0].task_id),
                plugin_id=manifest.get("plugin_id", ""),
                command=manifest.get("command", ""),
                created_at=manifest.get("created_at",
                                        records[0].t_wall.isoformat()),
                run_dir=run_dir.name,
            )
            for entry in records:
                self.apply_record(entry)
            count += 1
        return count

    @staticmethod
    def _read_manifest(run_dir: Path) -> dict:
        try:
            return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("no readable manifest in {}", run_dir)
            return {}
