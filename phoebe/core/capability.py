"""Capability layer: declarative registration + single validation choke point
(refactor.md §4.5).

Capability describes what an experiment needs; a handler is one model's
implementation; the per-controller registry is the live inventory of what a
connected device actually provides.  ``CapabilityRegistry.invoke`` is the ONE
place where requests (local objects or serialized dicts) and responses are
schema-validated.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import TypeAdapter

from .contracts import CapabilityId, ContractModel, TaskId, validate_boundary
from .errors import (
    CancelledByUser,
    CapabilityContractError,
    UnsupportedCapabilityError,
)

RequestT = TypeVar("RequestT", bound=ContractModel)
ResponseT = TypeVar("ResponseT")


class CancellationToken:
    """Cooperative cancellation reaching into in-flight controller waits."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    def cancel(self, reason: str | None = None) -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledByUser(self.reason or "cancelled")

    async def wait(self) -> None:
        await self._event.wait()


@runtime_checkable
class InvocationContext(Protocol):
    """What controllers may rely on from any caller (a run or the system)."""

    @property
    def task_id(self) -> TaskId | None: ...

    def ensure_not_cancelled(self) -> None: ...


class SystemContext:
    """Minimal context for framework-initiated calls (health checks, staging)."""

    def __init__(self, task_id: TaskId | None = None) -> None:
        self._task_id = task_id

    @property
    def task_id(self) -> TaskId | None:
        return self._task_id

    def ensure_not_cancelled(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Capability(Generic[RequestT, ResponseT]):
    """Code-level constant describing one versioned, validatable operation.

    id naming: ``org.lab.<kind>.<group>.v<N>.<op>`` — schema evolution adds a
    v2 id alongside v1 instead of changing semantics in place.
    """

    id: CapabilityId
    request_type: type[RequestT]
    response_adapter: TypeAdapter[ResponseT]
    requires_exclusive_lock: bool = True


Handler = Callable[[Any, InvocationContext], Awaitable[Any]]


class CapabilityRegistry:
    """Per-controller registry; the single validation choke point."""

    def __init__(self, owner: str, *, validate_responses: bool = True) -> None:
        self._owner = owner
        self._handlers: dict[CapabilityId, tuple[Capability, Handler, str]] = {}
        self.validate_responses = validate_responses

    def register(self, cap: Capability, handler: Handler, *, provider: str) -> None:
        if cap.id in self._handlers:
            raise ValueError(f"duplicate capability {cap.id} on {self._owner}")
        self._handlers[cap.id] = (cap, handler, provider)

    def supports(self, cap: Capability) -> bool:
        return cap.id in self._handlers

    def supports_id(self, cap_id: str) -> bool:
        return cap_id in self._handlers

    def list_ids(self) -> tuple[CapabilityId, ...]:
        return tuple(self._handlers.keys())

    async def invoke(
        self,
        cap: Capability[RequestT, ResponseT],
        request: RequestT | dict,
        context: InvocationContext,
    ) -> ResponseT:
        entry = self._handlers.get(cap.id)
        if entry is None:
            raise UnsupportedCapabilityError(str(cap.id), owner=self._owner)
        _, handler, _ = entry
        if isinstance(request, dict):                      # remote / serialized entry
            request = validate_boundary(cap.request_type, request)
        elif not isinstance(request, cap.request_type):    # local type misuse
            raise CapabilityContractError(str(cap.id), type(request))
        context.ensure_not_cancelled()
        response = await handler(request, context)
        if self.validate_responses:
            response = cap.response_adapter.validate_python(response)
        return response


__all__ = [
    "Capability",
    "CapabilityRegistry",
    "CancellationToken",
    "CancelledByUser",
    "InvocationContext",
    "SystemContext",
]
