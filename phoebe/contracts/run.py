"""Run lifecycle contracts: interactive FSM, journal records, catalog rows
(plan §6.2).

``RunState`` is the *interactive* state machine the UI renders.  The
``RunJournalRecord`` stream is the *persisted* truth: an append-only sequence
of lifecycle facts per run directory, on two independent axes —
``execution_outcome`` (what the plugin did) and ``finalized`` (whether cleanup
completed ok or degraded).  A crash leaves a journal without a ``finalized``
record; the startup recovery scan explains it without touching any device.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from .base import AwareDatetime, ContractModel, RunId, TaskId


class RunState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"  # leases held; run dir / writer / stage() in progress
    RUNNING = "running"
    PAUSING = "pausing"      # pause requested, takes effect at next checkpoint
    PAUSED = "paused"
    STOPPING = "stopping"    # cancel requested, hardware stop + cleanup running
    FINALIZING = "finalizing"  # outcome decided; stop/unstage/writer-close running
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"      # cancelled by user

    @property
    def is_terminal(self) -> bool:
        return self in (RunState.COMPLETED, RunState.FAILED, RunState.ABORTED)


ExecutionOutcome = Literal["completed", "failed", "aborted"]
FinalizedResult = Literal["ok", "degraded"]
RecoveryResolution = Literal["interrupted", "operator_review_required"]


class RunManifest(ContractModel):
    run_id: RunId
    task_id: TaskId
    plugin_id: str
    command: str
    created_at: AwareDatetime
    config_json: str                 # full experiment config, verbatim
    config_hash: str
    app_config_hash: str = ""
    git_commit: str = ""
    git_dirty: bool = False
    code_version: str = ""
    instruments: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DataPointer(ContractModel):
    run_id: RunId
    dataset: str
    index: int


class JournalRecordType(StrEnum):
    """Lifecycle facts, in causal order (plan §6.2).  ``RECOVERED`` is
    appended by the startup scan when a journal ends without ``FINALIZED``."""

    ADMITTED = "admitted"
    RUN_DIR_CREATED = "run_dir_created"
    BASELINE_CAPTURED = "baseline_captured"
    STAGED = "staged"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_OUTCOME = "execution_outcome"
    CLEANUP_STARTED = "cleanup_started"
    WRITER_CLOSED = "writer_closed"
    LEASES_RELEASED = "leases_released"
    FINALIZED = "finalized"
    RECOVERED = "recovered"


class RunJournalRecord(ContractModel):
    record: JournalRecordType
    task_id: TaskId
    run_id: RunId
    t_wall: AwareDatetime
    t_mono_ns: int
    outcome: ExecutionOutcome | None = None       # EXECUTION_OUTCOME records
    finalized: FinalizedResult | None = None      # FINALIZED records
    resolution: RecoveryResolution | None = None  # RECOVERED records
    detail: str | None = None


class RunResult(ContractModel):
    """Catalog row: one line per run, queryable without opening run dirs.
    ``state`` is the last known interactive state or a recovery resolution."""

    run_id: RunId
    task_id: TaskId
    plugin_id: str
    command: str
    created_at: AwareDatetime
    run_dir: str
    state: str
    execution_outcome: ExecutionOutcome | None = None
    finalized: FinalizedResult | None = None


class RecoveryReport(ContractModel):
    """One incomplete run explained by the startup scan (plan §6.2): what the
    journal proves happened, and what the operator should conclude."""

    run_id: RunId
    task_id: TaskId
    run_dir: str
    last_record: JournalRecordType
    resolution: RecoveryResolution
    explanation: str
