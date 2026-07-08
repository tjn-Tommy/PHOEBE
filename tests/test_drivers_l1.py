"""L1 unit tests: driver command formatting/parsing over MockScpiTransport
(refactor.md §14.1)."""
from __future__ import annotations

import numpy as np
import pytest

from phoebe.core.capability import SystemContext
from phoebe.core.contracts import InstrumentId
from phoebe.domain.spectrum import PeakSearchRequest, SpectrumScanConfig, TraceRequest
from phoebe.instruments.yokogawa_aq637x.controller import (
    AQ637XController,
    OSA_FIND_PEAKS,
    OsaOptions,
)
from phoebe.instruments.yokogawa_aq637x.driver import AQ637XDriver
from phoebe.transports.mock import MockScpiTransport


def _make_osa(rules: dict[str, str]) -> tuple[AQ637XController, MockScpiTransport]:
    transport = MockScpiTransport(rules)
    driver = AQ637XDriver(transport)
    options = OsaOptions(sweep_timeout_s=5.0, poll_interval_s=0.01)
    controller = AQ637XController(InstrumentId("osa.test"), driver, transport,
                                  options)
    return controller, transport


@pytest.fixture()
def osa_rules() -> dict[str, str]:
    n = 11
    x_m = ", ".join(f"{(778e-9 + i * 1e-10):.6e}" for i in range(n))
    y = ", ".join(f"{-70 + 5 * (i == 5):.2f}" for i in range(n))
    return {
        "*IDN?": "YOKOGAWA,AQ6370D,90Y1234,02.08",
        ":STATus:OPERation:EVENt?": "1",
        f":TRACe:X? TRA": f"{n}, {x_m}",
        f":TRACe:Y? TRA": f"{n}, {y}",
    }


async def test_aq637x_acquire_trace_commands_and_parse(osa_rules):
    controller, transport = _make_osa(osa_rules)
    await controller.connect()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=11,
                              sensitivity="high2")
    trace = await controller.acquire_trace(TraceRequest(scan=scan),
                                           context=SystemContext())
    writes = transport.commands("write")
    assert "CFORM1" in writes
    assert ":SENSe:WAVelength:CENTer 778.000000NM" in writes
    assert ":SENSe:WAVelength:SPAN 8.000000NM" in writes
    assert ":SENSe:SENSe HIGH2" in writes
    assert ":SENSe:SWEEp:POINts 11" in writes
    assert ":INITiate" in writes
    # count header dropped, meters → nm
    assert trace.x_nm.shape == (11,)
    assert abs(trace.x_nm[0] - 778.0) < 1e-6
    assert trace.y_dbm[5] == pytest.approx(-65.0)


async def test_aq637x_software_averaging(osa_rules):
    controller, transport = _make_osa(osa_rules)
    await controller.connect()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=11,
                              average_count=3)
    trace = await controller.acquire_trace(TraceRequest(scan=scan),
                                           context=SystemContext())
    assert trace.meta.averages == 3
    assert transport.commands("write").count(":INITiate") == 3


async def test_aq637x_find_peaks_capability(osa_rules):
    controller, _ = _make_osa(osa_rules)
    await controller.connect()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=11)
    await controller.acquire_trace(TraceRequest(scan=scan), context=SystemContext())
    peaks = await controller.capabilities.invoke(
        OSA_FIND_PEAKS, PeakSearchRequest(threshold_dbm=-68.0), SystemContext())
    assert len(peaks) == 1
    assert peaks[0].power_dbm == pytest.approx(-65.0)


async def test_aq637x_capability_dict_request_is_validated(osa_rules):
    controller, _ = _make_osa(osa_rules)
    await controller.connect()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=11)
    await controller.acquire_trace(TraceRequest(scan=scan), context=SystemContext())
    # dict payload (as a future gRPC entry would send) passes through
    # model_validate at the registry choke point
    peaks = await controller.capabilities.invoke(
        OSA_FIND_PEAKS, {"threshold_dbm": -68.0, "max_peaks": 4}, SystemContext())
    assert peaks
    with pytest.raises(Exception):
        await controller.capabilities.invoke(
            OSA_FIND_PEAKS, {"threshold_dbm": "not-a-number"}, SystemContext())


async def test_aq637x_stop_sends_abort(osa_rules):
    controller, transport = _make_osa(osa_rules)
    await controller.connect()
    await controller.stop()
    assert ":ABORt" in transport.commands("write")
