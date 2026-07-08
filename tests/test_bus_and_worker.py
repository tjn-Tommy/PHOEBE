"""EventBus fan-out/drop/retained semantics (§9) and worker threads (§12.3)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from phoebe.core.bus import DropPolicy, EventBus, ThrottledEmitter
from phoebe.core.contracts import timestamps
from phoebe.core.errors import BusOverflowError
from phoebe.core.events import ProgressEvent, RunState, RunStateEvent
from phoebe.core.worker import BlockingDeviceWorker, WorkerPool


def _progress(step: int) -> ProgressEvent:
    return ProgressEvent(step=step, **timestamps())


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


async def test_error_policy_raises_on_overflow():
    bus = EventBus()
    bus.bind_loop()
    bus.subscribe(["progress"], maxsize=1, policy=DropPolicy.ERROR)
    bus.publish(_progress(0))
    with pytest.raises(BusOverflowError):
        bus.publish(_progress(1))


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
