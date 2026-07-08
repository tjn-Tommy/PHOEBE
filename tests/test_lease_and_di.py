"""Lease atomicity/inheritance (§6) and DI resolution (§7)."""
from __future__ import annotations

import pytest

from phoebe.core.contracts import InstrumentId, TaskId
from phoebe.core.di import DependencyResolver, Depends, ResolvedRequirement
from phoebe.core.errors import LeaseUnavailableError, PhoebeConfigError
from phoebe.instruments.protocols import PatternModulator, SpectrumAnalyzer


def _req(name: str, iid: str, kind: str = "spectrum_analyzer") -> ResolvedRequirement:
    return ResolvedRequirement(name, InstrumentId(iid), kind)


@pytest.fixture()
def dm(sim_runtime):
    return sim_runtime.device_manager


@pytest.fixture()
async def sim_runtime():
    from phoebe.app.bootstrap import build_runtime
    from phoebe.core.config import parse_app_config

    cfg = parse_app_config({
        "instruments": [
            {"instrument_id": "osa.main", "kind": "spectrum_analyzer",
             "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
             "backend": "sim",
             "connection": {"transport": "tcp", "host": "x", "port": 10001}},
            {"instrument_id": "slm.primary", "kind": "pattern_modulator",
             "vendor": "santec", "model": "slm-200", "role": "primary_slm",
             "backend": "sim",
             "connection": {"transport": "vendor_dll", "dll_path": "x"},
             "options": {"settle_ms": 1.0, "height": 60, "width": 80}},
        ],
    })
    runtime = await build_runtime(cfg, start_reaper=False)
    yield runtime
    await runtime.shutdown()


async def test_atomic_all_or_nothing(dm):
    t1, t2 = TaskId("task_1"), TaskId("task_2")
    reqs = [_req("osa", "osa.main"), _req("slm", "slm.primary", "pattern_modulator")]
    leases = dm.try_acquire_all(t1, [reqs[0]])
    assert leases.holds(InstrumentId("osa.main"))

    # t2 wants both; OSA is taken → NOTHING is granted (SLM stays free)
    with pytest.raises(LeaseUnavailableError):
        dm.try_acquire_all(t2, reqs)
    assert dm.owner_of(InstrumentId("slm.primary")) is None

    dm.release(t1, leases)
    assert dm.active_lease_count() == 0


async def test_lease_inheritance_refcount(dm):
    t1 = TaskId("task_parent")
    parent = dm.try_acquire_all(t1, [_req("osa", "osa.main")])

    # child flow re-declares the same OSA: satisfied from the parent context
    child = dm.try_acquire_all(TaskId("task_child"), [_req("osa", "osa.main")],
                               parent=parent)
    assert child.holds(InstrumentId("osa.main"))

    # child release only decrements; the parent still owns the device
    dm.release(TaskId("task_child"), child)
    assert dm.owner_of(InstrumentId("osa.main")) is not None
    dm.release(t1, parent)
    assert dm.owner_of(InstrumentId("osa.main")) is None


async def test_di_resolution_by_role_binding_and_uniqueness(sim_runtime):
    resolver = DependencyResolver(
        role_map=sim_runtime.device_manager.role_map(),
        kind_index=sim_runtime.device_manager.kind_index(),
        plugin_bindings={"p1": {"spectro": "main_osa"}},
    )

    async def fn_role(config, ctx,
                      osa: SpectrumAnalyzer = Depends(role="main_osa")):
        ...

    async def fn_binding(config, ctx, spectro: SpectrumAnalyzer = Depends()):
        ...

    async def fn_unique(config, ctx, slm: PatternModulator = Depends()):
        ...

    assert resolver.resolve("p1", fn_role)[0].instrument_id == "osa.main"
    assert resolver.resolve("p1", fn_binding)[0].instrument_id == "osa.main"
    assert resolver.resolve("p1", fn_unique)[0].instrument_id == "slm.primary"


async def test_di_unknown_role_fails_fast(sim_runtime):
    resolver = DependencyResolver(
        role_map=sim_runtime.device_manager.role_map(),
        kind_index=sim_runtime.device_manager.kind_index(),
        plugin_bindings={},
    )

    async def fn(config, ctx, osa: SpectrumAnalyzer = Depends(role="nope")):
        ...

    with pytest.raises(PhoebeConfigError):
        resolver.resolve("p", fn)
