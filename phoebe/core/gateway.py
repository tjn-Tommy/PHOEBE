"""Gateway: the must-deliver command channel (refactor.md §13).

Everything the UI sends is a ``CommandEnvelope``; the gateway deserializes,
routes via the command registry and hands off to the TaskManager.  Commands
and events never share a queue — the point-to-point command path cannot be
crowded out by the droppable observation stream.

``pause`` / ``resume`` / ``cancel`` are platform built-ins that go straight
to the TaskManager state machine, bypassing plugins.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import Field

from .contracts import AwareDatetime, ContractModel, TaskId, utc_now

if TYPE_CHECKING:
    from .task_manager import TaskManager


class CommandEnvelope(ContractModel):
    command_id: str
    command: str                       # "start_tpa_run" / "pause" / "cancel"
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str = "local_ui"
    t_wall: AwareDatetime = Field(default_factory=utc_now)


class CommandAck(ContractModel):
    command_id: str
    accepted: bool
    task_id: TaskId | None = None
    queued: bool = False
    reason: str | None = None          # e.g. "423 locked by task_..."


BUILTIN_COMMANDS = frozenset({"pause", "resume", "cancel"})


class Gateway:
    def __init__(self, task_manager: "TaskManager") -> None:
        self._tm = task_manager

    async def submit(self, envelope: CommandEnvelope) -> CommandAck:
        if envelope.command in BUILTIN_COMMANDS:
            return await self._builtin(envelope)
        return await self._tm.dispatch(envelope)

    def submit_threadsafe(self, envelope: CommandEnvelope,
                          loop: asyncio.AbstractEventLoop) -> "asyncio.Future[CommandAck]":
        """Entry point for the Qt thread: schedules submit() onto the loop."""
        return asyncio.run_coroutine_threadsafe(self.submit(envelope), loop)  # type: ignore[return-value]

    async def _builtin(self, envelope: CommandEnvelope) -> CommandAck:
        raw = envelope.payload.get("task_id")
        if not isinstance(raw, str) or not raw:
            return CommandAck(command_id=envelope.command_id, accepted=False,
                              reason="payload.task_id (str) is required")
        task_id = TaskId(raw)
        try:
            if envelope.command == "pause":
                self._tm.request_pause(task_id)
            elif envelope.command == "resume":
                self._tm.request_resume(task_id)
            else:
                self._tm.request_cancel(task_id)
        except KeyError:
            return CommandAck(command_id=envelope.command_id, accepted=False,
                              reason=f"unknown task {task_id}")
        except Exception as exc:                    # invalid state transition etc.
            return CommandAck(command_id=envelope.command_id, accepted=False,
                              reason=str(exc))
        return CommandAck(command_id=envelope.command_id, accepted=True,
                          task_id=task_id)
