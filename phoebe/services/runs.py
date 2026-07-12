"""Run service: submit/control commands, query the run catalog + journals."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..contracts.commands import CommandAck, CommandEnvelope
from ..contracts.run import RunJournalRecord, RunResult
from ..core.journal import read_journal

if TYPE_CHECKING:
    from ..contracts.base import RunId, TaskId
    from ..core.catalog import RunCatalog
    from ..core.gateway import Gateway
    from ..core.task_manager import TaskManager


class RunService:
    def __init__(self, *, gateway: Gateway, task_manager: TaskManager,
                 catalog: RunCatalog | None, runs_root: Path) -> None:
        self._gateway = gateway
        self._tm = task_manager
        self._catalog = catalog
        self._runs_root = runs_root

    # ------------------------------------------------------------- commands
    async def submit(self, envelope: CommandEnvelope) -> CommandAck:
        return await self._gateway.submit(envelope)

    async def pause(self, task_id: TaskId | str) -> CommandAck:
        return await self._builtin("pause", task_id)

    async def resume(self, task_id: TaskId | str) -> CommandAck:
        return await self._builtin("resume", task_id)

    async def cancel(self, task_id: TaskId | str) -> CommandAck:
        return await self._builtin("cancel", task_id)

    async def _builtin(self, command: str, task_id: TaskId | str) -> CommandAck:
        return await self._gateway.submit(CommandEnvelope(
            command_id=f"svc-{uuid.uuid4().hex[:8]}", command=command,
            payload={"task_id": str(task_id)}))

    # -------------------------------------------------------------- queries
    async def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[RunResult]:
        if self._catalog is None:
            return []
        return self._catalog.list_runs(limit=limit, offset=offset)

    async def get_run(self, run_id: RunId | str) -> RunResult | None:
        if self._catalog is None:
            return None
        return self._catalog.get(run_id)

    async def read_run_journal(self, run_id: RunId | str) -> list[RunJournalRecord]:
        """Journal projection of one run (plan §6.7 ``GET /runs/{id}``)."""
        result = await self.get_run(run_id)
        if result is None:
            return []
        return read_journal(self._runs_root / result.run_dir)

    async def active_tasks(self) -> tuple[TaskId, ...]:
        return self._tm.active_tasks()
