"""EventBus fan-out/drop/retained semantics (§9; v2 per plan §6.5) and worker
threads (§12.3)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from phoebe.core.bus import DropPolicy, EventBus, ThrottledEmitter
from phoebe.core.contracts import InstrumentId, TaskId, timestamps
from phoebe.core.errors import BusOverflowError
from phoebe.core.events import (
    DeviceHealthEvent,
    LogEvent,
    ProgressEvent,
    RunState,
    RunStateEvent,
)
from phoebe.core.worker import BlockingDeviceWorker, WorkerPool


def _progress(step: int) -> ProgressEvent:
    return ProgressEvent(step=step, **timestamps())


def _health(iid: str, status: str = "ok") -> DeviceHealthEvent:
    return DeviceHealthEvent(instrument_id=InstrumentId(iid), status=status,
                             **timestamps())


def _run_state(task: str, state: RunState) -> RunStateEvent:
    return RunStateEvent(task_id=TaskId(task), state=state, **timestamps())


async def test_fanout_and_seq_stamping():
    bus = EventBus()
    bus.bind_loop()
    sub_a = bus.subscribe(["progress"])
    sub_b = bus.subscribe(["progress"])
    bus.publish(_progress(1))
    ev_a = await sub_a.get()
    ev_b = await sub_b.get()
    assert ev_a.step == ev_b.step == 1
    assert ev_a.seq == ev_b.seq > 0


async def test_retained_event_for_late_subscriber():
    bus = EventBus()
    bus.bind_loop()
    bus.publish(RunStateEvent(state=RunState.RUNNING, **timestamps()))
    late = bus.subscribe(["run_state"])
    ev = await late.get()
    assert ev.state is RunState.RUNNING


async def test_drop_oldest_counts_drops():
    bus = EventBus()
    bus.bind_loop()
    sub = bus.subscribe(["progress"], maxsize=2, policy=DropPolicy.DROP_OLDEST)
    for i in range(5):
        bus.publish(_progress(i))
    assert sub.dropped == 3
    assert (await sub.get()).step == 3       # oldest were dropped
    assert bus.total_dropped() == 3


async def test_error_policy_fails_subscription_not_publisher():
    """Plan §6.5: an ERROR-policy overflow detaches the subscription and is
    counted; the publisher (and every other subscriber) is untouched."""
    bus = EventBus()
    bus.bind_loop()
    fragile = bus.subscribe(["progress"], maxsize=1, policy=DropPolicy.ERROR)
    healthy = bus.subscribe(["progress"], maxsize=64)
    bus.publish(_progress(0))
    bus.publish(_progress(1))          # overflows `fragile` — must NOT raise here
    bus.publish(_progress(2))
    assert fragile.failed
    assert bus.stats().failed_subscriptions == 1
    # the failed consumer observes the overflow as its own error
    with pytest.raises(BusOverflowError):
        while True:
            await asyncio.wait_for(fragile.get(), 1.0)
    # the healthy subscriber saw everything
    steps = [(await healthy.get()).step for _ in range(3)]
    assert steps == [0, 1, 2]


async def test_per_entity_retained_snapshot_for_late_subscriber():
    """Reconnect snapshot (plan C-1 acceptance): a late subscriber receives
    one retained event per entity — every device, every task — in seq order."""
    bus = EventBus()
    bus.bind_loop()
    bus.publish(_health("osa.main", "ok"))
    bus.publish(_health("slm.primary", "degraded"))
    bus.publish(_health("osa.main", "error"))          # newer state, same entity
    bus.publish(_run_state("task_a", RunState.RUNNING))
    bus.publish(_run_state("task_b", RunState.COMPLETED))

    late = bus.subscribe(["device_health", "run_state"], maxsize=64)
    snapshot = []
    while (ev := late.get_nowait()) is not None:
        snapshot.append(ev)
    healths = {str(e.instrument_id): e.status for e in snapshot
               if e.event_type == "device_health"}
    states = {str(e.task_id): e.state for e in snapshot
              if e.event_type == "run_state"}
    assert healths == {"osa.main": "error", "slm.primary": "degraded"}
    assert states == {"task_a": RunState.RUNNING, "task_b": RunState.COMPLETED}
    assert [e.seq for e in snapshot] == sorted(e.seq for e in snapshot)


async def test_replay_since_seq():
    bus = EventBus()
    bus.bind_loop()
    for i in range(5):
        bus.publish(_progress(i))
    cutoff = bus.current_seq                            # after step 4
    bus.publish(_progress(5))
    bus.publish(_health("osa.main"))

    replayed = bus.replay_since(cutoff)
    assert [getattr(e, "step", None) for e in replayed] == [5, None]
    only_progress = bus.replay_since(cutoff, topics=["progress"])
    assert [e.step for e in only_progress] == [5]

    # subscribe(since_seq=...) primes the queue from the ring, not the snapshot
    sub = bus.subscribe(["progress"], since_seq=cutoff)
    assert (sub.get_nowait()).step == 5
    assert sub.get_nowait() is None


async def test_oversize_event_is_dropped_and_counted_in_prod(monkeypatch):
    """Plan §6.5: the 64 KB ceiling is a real check.  Schema caps make a
    genuinely oversized event unconstructible, so the ceiling itself is
    lowered to drive the enforcement path."""
    import phoebe.core.bus as bus_mod

    bus = EventBus(dev_mode=False)
    bus.bind_loop()
    sub = bus.subscribe(["log", "progress"], maxsize=8)
    bus.publish(LogEvent(message="small", **timestamps()))
    monkeypatch.setattr(bus_mod, "MAX_EVENT_JSON_BYTES", 64)
    bus.publish(_progress(3))                            # > 64 bytes → dropped
    assert bus.stats().oversize_dropped == 1
    got = []
    while (ev := sub.get_nowait()) is not None:
        got.append(ev)
    assert [e.event_type for e in got] == ["log"]        # progress never delivered


async def test_oversize_event_raises_in_dev_mode(monkeypatch):
    import phoebe.core.bus as bus_mod

    bus = EventBus(dev_mode=True)
    bus.bind_loop()
    monkeypatch.setattr(bus_mod, "MAX_EVENT_JSON_BYTES", 64)
    with pytest.raises(ValueError, match="RunWriter"):
        bus.publish(_progress(1))
    assert bus.stats().oversize_dropped == 1


async def test_threadsafe_publish_from_worker_thread():
    bus = EventBus()
    bus.bind_loop()
    sub = bus.subscribe(["progress"])

    def other_thread() -> None:
        bus.publish_threadsafe(_progress(42))

    threading.Thread(target=other_thread).start()
    ev = await asyncio.wait_for(sub.get(), timeout=2)
    assert ev.step == 42


async def test_throttle_trailing_edge_keeps_last():
    bus = EventBus()
    bus.bind_loop()
    sub = bus.subscribe(["progress"], maxsize=64)
    throttle = ThrottledEmitter(bus, min_interval_s=0.05)
    for i in range(10):
        throttle.emit(_progress(i))
    await asyncio.sleep(0.12)
    got = []
    while (ev := sub.get_nowait()) is not None:
        got.append(ev.step)
    assert got[0] == 0                        # leading edge published immediately
    assert got[-1] == 9                       # trailing edge kept the newest
    assert len(got) < 10                      # intermediate events were coalesced


async def test_worker_runs_calls_on_one_thread():
    worker = BlockingDeviceWorker("test-dev")
    thread_ids = set()

    def job(x: int) -> int:
        thread_ids.add(threading.get_ident())
        return x * 2

    results = [await worker.call(job, i) for i in range(5)]
    assert results == [0, 2, 4, 6, 8]
    assert len(thread_ids) == 1
    assert threading.get_ident() not in thread_ids
    worker.stop()


async def test_worker_propagates_exceptions():
    worker = BlockingDeviceWorker("test-dev-exc")

    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await worker.call(boom)
    worker.stop()


async def test_worker_initializer_failure_surfaces_on_call():
    def bad_init() -> None:
        raise OSError("dll missing")

    worker = BlockingDeviceWorker("bad-init", initializer=bad_init)
    worker._init_done.wait(2)
    with pytest.raises(RuntimeError, match="failed to initialize"):
        await worker.call(lambda: 1)
    worker.stop()


async def test_worker_pool_one_worker_per_device():
    pool = WorkerPool()
    w1 = pool.for_device("dev-a")
    w2 = pool.for_device("dev-a")
    w3 = pool.for_device("dev-b")
    assert w1 is w2
    assert w1 is not w3
    pool.stop_all()
