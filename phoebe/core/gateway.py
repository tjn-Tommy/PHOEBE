"""Gateway: the must-deliver command channel (refactor.md §13).

Everything the UI sends is a ``CommandEnvelope``; the gateway deserializes,
routes via the command registry and hands off to the TaskManager.  Commands
and events never share a queue — the point-to-point command path cannot be
crowded out by the droppable observation stream.

``pause`` / ``resume`` / ``cancel`` are platform built-ins that go straight
to the TaskManager state machine, bypassing plugins.  Their acks carry the
same typed ``AckCode`` vocabulary as dispatch acks (plan §6.4).

The command/ack models moved to ``phoebe.contracts.commands`` (plan §7
promotion); they are re-exported here for pre-promotion import paths.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..contracts.commands import (
    AckCode,
    AdmissionDecision,
    CommandAck,
    CommandEnvelope,
    ack_from_decision,
)
from .contracts import TaskId

if TYPE_CHECKING:
    from .task_manager import TaskManager

__all__ = [
    "AckCode",
    "AdmissionDecision",
    "BUILTIN_COMMANDS",
    "CommandAck",
    "CommandEnvelope",
    "Gateway",
    "ack_from_decision",
]

BUILTIN_COMMANDS = frozenset({"pause", "resume", "cancel"})


class Gateway:
    def __init__(self, task_manager: TaskManager) -> None:
        self._tm = task_manager

    async def submit(self, envelope: CommandEnvelope) -> CommandAck:
        if envelope.command in BUILTIN_COMMANDS:
            return await self._builtin(envelope)
        return await self._tm.dispatch(envelope)

    def submit_threadsafe(self, envelope: CommandEnvelope,
                          loop: asyncio.AbstractEventLoop) -> asyncio.Future[CommandAck]:
        """Entry point for the Qt thread: schedules submit() onto the loop."""
        return asyncio.run_coroutine_threadsafe(self.submit(envelope), loop)  # type: ignore[return-value]

    async def _builtin(self, envelope: CommandEnvelope) -> CommandAck:
        raw = envelope.payload.get("task_id")
        if not isinstance(raw, str) or not raw:
            return CommandAck(command_id=envelope.command_id, accepted=False,
                              code=AckCode.INVALID_PAYLOAD,
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
                              code=AckCode.UNKNOWN_TASK,
                              reason=f"unknown task {task_id}")
        except Exception as exc:                    # invalid state transition etc.
            return CommandAck(command_id=envelope.command_id, accepted=False,
                              code=AckCode.INVALID_STATE, reason=str(exc))
        return CommandAck(command_id=envelope.command_id, accepted=True,
                          code=AckCode.ACCEPTED, task_id=task_id)
