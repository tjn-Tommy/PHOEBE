"""Resource model: leases with atomic acquisition and inheritance
(refactor.md §6).

A lease is a row in DeviceManager's ownership table, not an asyncio.Lock —
acquisition is a synchronous, await-free operation and therefore atomic on a
single event loop; crashed holders are reclaimed from the table by TTL.
LeaseSet adds reference counting so sub-flows can inherit a parent run's
leases instead of deadlocking against them.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from .contracts import (
    AwareDatetime,
    ContractModel,
    InstrumentId,
    LeaseId,
    Seconds,
    TaskId,
    utc_now,
)

_lease_counter = itertools.count(1)


def new_lease_id() -> LeaseId:
    return LeaseId(f"lease_{next(_lease_counter):06d}")


class Lease(ContractModel):
    lease_id: LeaseId
    instrument_id: InstrumentId
    holder_task_id: TaskId
    parent_lease_id: LeaseId | None = None
    acquired_at: AwareDatetime
    ttl_s: Seconds = 600.0


@dataclass
class _Slot:
    lease: Lease
    refcount: int
    touched_mono: float          # last heartbeat, loop-time seconds


class LeaseSet:
    """All leases one run holds, with per-instrument reference counts.

    A child flow resolved against a parent context increments the count; the
    physical release happens when the root run's cleanup drops it to zero.
    """

    def __init__(self) -> None:
        self._slots: dict[InstrumentId, _Slot] = {}

    # -- queries ---------------------------------------------------------------
    def holds(self, instrument_id: InstrumentId) -> bool:
        return instrument_id in self._slots

    def lease_for(self, instrument_id: InstrumentId) -> Lease:
        return self._slots[instrument_id].lease

    def instrument_ids(self) -> tuple[InstrumentId, ...]:
        return tuple(self._slots.keys())

    def __len__(self) -> int:
        return len(self._slots)

    # -- mutation (called only by DeviceManager on the loop thread) -----------
    def add(self, lease: Lease, *, now_mono: float) -> None:
        self._slots[lease.instrument_id] = _Slot(lease, 1, now_mono)

    def incref(self, instrument_id: InstrumentId) -> None:
        self._slots[instrument_id].refcount += 1

    def decref(self, instrument_id: InstrumentId) -> int:
        slot = self._slots[instrument_id]
        slot.refcount -= 1
        if slot.refcount <= 0:
            del self._slots[instrument_id]
            return 0
        return slot.refcount

    def touch_all(self, now_mono: float) -> None:
        for slot in self._slots.values():
            slot.touched_mono = now_mono

    def expired(self, now_mono: float) -> list[Lease]:
        return [
            slot.lease
            for slot in self._slots.values()
            if now_mono - slot.touched_mono > slot.lease.ttl_s
        ]

    @staticmethod
    def merge(parent: "LeaseSet | None", granted: "list[Lease]",
              *, now_mono: float) -> "LeaseSet":
        """View combining inherited parent leases with newly granted ones.

        The returned set SHARES the parent's slots (refcounts are common), so
        releasing the child view decrefs the parent's counts as intended.
        """
        if parent is None:
            result = LeaseSet()
            for lease in granted:
                result.add(lease, now_mono=now_mono)
            return result
        child = LeaseSet()
        child._slots = parent._slots            # shared table, shared refcounts
        for lease in granted:
            child.add(lease, now_mono=now_mono)
        return child


def make_lease(task_id: TaskId, instrument_id: InstrumentId,
               *, ttl_s: float, parent: LeaseId | None = None) -> Lease:
    return Lease(
        lease_id=new_lease_id(),
        instrument_id=instrument_id,
        holder_task_id=task_id,
        parent_lease_id=parent,
        acquired_at=utc_now(),
        ttl_s=ttl_s,
    )
