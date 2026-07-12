"""Instrument-stack and diagnostics suite (evolution plan Phase B, PRs B-3/B-4/B-5).

Covers: termchar-safe VISA binary reads (H8), TCP peer-close/timeout handling
in binary reads (H9), the Santec temp-CSV ledger (H10), worker relay
hardening (M-group), the loop watchdog + lag monitor (A5), the
single-instance lock (A7), and recording→replay transcript round-trips (B-5).
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from phoebe.app.single_instance import AnotherInstanceRunningError, SingleInstanceLock
from phoebe.core.contracts import InstrumentId
from phoebe.core.diagnostics import EventLoopDiagnostics
from phoebe.core.errors import (
    InstrumentConnectionError,
    InstrumentProtocolError,
    InstrumentTimeoutError,
)
from phoebe.core.worker import BlockingDeviceWorker
from phoebe.instruments.rs_rto6.driver import RTO6Driver
from phoebe.instruments.santec_slm200.driver import SlmDllDriver
from phoebe.instruments.yokogawa_aq637x.driver import AQ637XDriver
from phoebe.transports.mock import MockScpiTransport, TranscriptReplayTransport
from phoebe.transports.recording import RecordingScpiTransport
from phoebe.transports.tcp import TcpScpiTransport
from phoebe.transports.visa import VisaScpiTransport, read_ieee_block_bytes


# ------------------------------------------------------------------- H8 (VISA)
class _FakeTimeout(Exception):
    pass


class FakeVisaInst:
    """Minimal pyvisa MessageBasedResource stand-in: exact-count read_bytes
    served from a canned buffer; empty buffer raises like a VISA timeout."""

    def __init__(self, reply: bytes) -> None:
        self._buf = bytearray(reply)
        self.timeout = 5000
        self.written: list[str] = []

    def write(self, command: str) -> None:
        self.written.append(command)

    def read_bytes(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise _FakeTimeout("timeout: no more data")
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _visa_transport(reply: bytes) -> tuple[VisaScpiTransport, FakeVisaInst]:
    transport = VisaScpiTransport("FAKE::INSTR", worker=None)  # type: ignore[arg-type]
    inst = FakeVisaInst(reply)
    transport._inst = inst
    return transport, inst


def test_visa_binary_read_survives_newline_bytes_in_payload():
    """H8: a float32 payload contains 0x0A statistically every ~256 bytes;
    read_raw() honours the termchar on several backends and truncates there.
    The field-wise read_bytes() path must return the full block."""
    rng = np.random.default_rng(42)
    payload = rng.random(256, dtype=np.float32).tobytes()
    assert b"\n" in payload            # the test is vacuous otherwise
    reply = b"#" + str(len(str(len(payload)))).encode() \
        + str(len(payload)).encode() + payload + b"\n"
    transport, inst = _visa_transport(reply)
    got = transport._query_binary_blocking("CHANnel1:DATA?")
    assert got == payload
    assert inst.written == ["CHANnel1:DATA?"]


def test_visa_binary_read_skips_stale_terminator_and_rejects_garbage():
    payload = b"\x01\x02\x03\x04"
    inst = FakeVisaInst(b"\n#14" + payload + b"\n")   # stale '\n' from prior reply
    assert read_ieee_block_bytes(inst) == payload

    with pytest.raises(InstrumentProtocolError):
        read_ieee_block_bytes(FakeVisaInst(b"garbage"))
    with pytest.raises(InstrumentProtocolError):
        read_ieee_block_bytes(FakeVisaInst(b"#0rest\n"))   # indefinite length


# -------------------------------------------------------------------- H9 (TCP)
class FakeSocket:
    """Scripted socket: recv() serves chunks, then b'' forever (peer close);
    an entry may also be an exception instance to raise."""

    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""                        # closed peer: empty forever
        item = self._chunks[0]
        if isinstance(item, Exception):
            self._chunks.pop(0)
            raise item
        if len(item) > n:                     # partial read: keep the remainder
            self._chunks[0] = item[n:]
            return item[:n]
        self._chunks.pop(0)
        return item

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


def _tcp_transport(chunks: list[bytes | Exception]) -> tuple[TcpScpiTransport, FakeSocket]:
    transport = TcpScpiTransport("fake", 5025, worker=None)  # type: ignore[arg-type]
    sock = FakeSocket(chunks)
    transport._sock = sock
    return transport, sock


def test_tcp_binary_read_raises_on_peer_close_instead_of_busy_looping():
    """H9: the old header/length loops did bare recv() with no empty-chunk
    check — a peer close spun the worker thread at 100% CPU forever."""
    # peer sends half the header, then closes
    transport, sock = _tcp_transport([b"#"])
    with pytest.raises(InstrumentConnectionError, match="closed"):
        transport._query_binary_blocking("CHANnel1:DATA?")
    assert transport._sock is None            # dead handle dropped (invalidate)
    assert sock.closed

    # close mid-payload
    transport, _ = _tcp_transport([b"#", b"14", b"\x01\x02"])
    with pytest.raises(InstrumentConnectionError, match="closed"):
        transport._query_binary_blocking("CHANnel1:DATA?")


def test_tcp_binary_read_maps_socket_timeout():
    # socket.timeout IS TimeoutError since 3.10 — what a real recv() raises
    transport, _ = _tcp_transport([b"#3", TimeoutError("slow")])
    with pytest.raises(InstrumentTimeoutError):
        transport._query_binary_blocking("CHANnel1:DATA?")


def test_tcp_binary_read_full_block_roundtrip():
    payload = bytes(range(200)) * 3           # contains 0x0A bytes too
    transport, sock = _tcp_transport(
        [b"#", b"3", b"600", payload[:100], payload[100:], b"\n"])
    got = transport._query_binary_blocking("CHANnel1:DATA?")
    assert got == payload
    assert sock.sent == [b"CHANnel1:DATA?\r\n"]


# ----------------------------------------------------------------- H10 (Santec)
def test_santec_frame_csv_is_deleted_after_display(tmp_path, monkeypatch):
    """H10: one multi-MB CSV per frame leaked into %TEMP% forever — a 10k-step
    optimization run leaks ~90 GB.  The ledger deletes each file right after
    the DLL consumed it."""
    driver = SlmDllDriver("unused.dll")
    loaded: list[str] = []
    monkeypatch.setattr(driver, "load_csv", lambda p: loaded.append(p))

    frame = np.zeros((4, 6), dtype=np.uint16)
    path = driver.display_frame(frame)

    assert loaded, "DLL load was not invoked"
    assert not path.exists(), "temp CSV leaked after display_frame"
    assert not driver._temp_ledger, "ledger should be empty after a clean sweep"


def test_santec_csv_deleted_even_when_dll_load_fails(monkeypatch):
    driver = SlmDllDriver("unused.dll")

    def boom(_p):
        raise InstrumentConnectionError("dll rejected the file")

    monkeypatch.setattr(driver, "load_csv", boom)
    with pytest.raises(InstrumentConnectionError):
        driver.display_frame(np.zeros((2, 2), dtype=np.uint16))
    assert not driver._temp_ledger


# ----------------------------------------------------------- worker relay (M)
async def test_worker_survives_closed_caller_loop():
    """A result relayed to a closed loop (shutdown race) must not kill the
    worker thread — transports hold this worker forever."""
    worker = BlockingDeviceWorker("test-relay")
    try:
        dead_loop = asyncio.new_event_loop()
        fut = dead_loop.create_future()
        dead_loop.close()
        worker._jobs.put((lambda: 42, (), {}, fut, dead_loop))
        deadline = time.monotonic() + 5
        while not worker._jobs.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker.is_alive
        # and it still serves calls afterwards
        assert await worker.call(lambda: "still alive") == "still alive"
    finally:
        worker.stop()


# ------------------------------------------------------------ diagnostics (A5)
async def test_watchdog_dumps_stacks_when_loop_wedges(tmp_path):
    loop = asyncio.get_running_loop()
    dump_path = tmp_path / "stall_dump.txt"
    stalls: list[float] = []
    diag = EventLoopDiagnostics(loop, heartbeat_interval_s=0.02, lag_warn_s=10.0,
                                stall_dump_s=0.15, dump_path=dump_path,
                                on_stall=stalls.append)
    diag.start()
    try:
        await asyncio.sleep(0.1)              # heartbeat established
        deadline = time.monotonic() + 0.4     # deliberately WEDGE the loop
        while time.monotonic() < deadline:
            pass
        await asyncio.sleep(0.05)             # let the beat task catch up
    finally:
        diag.stop()
    assert stalls, "watchdog never fired on a wedged loop"
    text = dump_path.read_text(encoding="utf-8")
    assert "Thread" in text                    # faulthandler stack dump present
    assert diag.max_lag_s > 0.1                # lag monitor saw the wedge
    assert diag.stall_count == 1               # one dump per stall episode


# --------------------------------------------------------- single instance (A7)
def test_second_instance_is_refused(tmp_path):
    lock_path = tmp_path / ".phoebe.lock"
    first = SingleInstanceLock(lock_path).acquire()
    try:
        with pytest.raises(AnotherInstanceRunningError):
            SingleInstanceLock(lock_path).acquire()
    finally:
        first.release()
    # released lock is reusable (no stale-lock problem)
    with SingleInstanceLock(lock_path):
        pass


# ------------------------------------------------- recording → replay (B-5, L2)
def _osa_rules() -> dict[str, str]:
    n = 11
    x_m = ", ".join(f"{(778e-9 + i * 1e-10):.6e}" for i in range(n))
    y = ", ".join(f"{-70 + 5 * (i == 5):.2f}" for i in range(n))
    return {
        "*IDN?": "YOKOGAWA,AQ6370D,90Y1234,02.08",
        ":STATus:OPERation:EVENt?": "1",
        ":TRACe:X? TRA": f"{n}, {x_m}",
        ":TRACe:Y? TRA": f"{n}, {y}",
    }


async def _drive_osa(transport) -> np.ndarray:
    from phoebe.domain.spectrum import SpectrumScanConfig, TraceRequest
    from phoebe.instruments.yokogawa_aq637x.controller import (
        AQ637XController,
        OsaOptions,
    )
    driver = AQ637XDriver(transport)
    controller = AQ637XController(
        InstrumentId("osa.test"), driver, transport,
        OsaOptions(sweep_timeout_s=5.0, poll_interval_s=0.01))
    await controller.connect()
    scan = SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=11,
                              sensitivity="high2")
    from phoebe.core.capability import SystemContext
    trace = await controller.acquire_trace(TraceRequest(scan=scan),
                                           context=SystemContext())
    return trace.y_dbm


async def test_aq637x_record_then_replay_roundtrip(tmp_path):
    """B-5 acceptance shape: record a session, replay it, assert exhaustion.
    (Recorded here against the L1 mock; the same harness replays transcripts
    recorded on real hardware once they exist.)"""
    recorder = RecordingScpiTransport(MockScpiTransport(_osa_rules()),
                                      redact=lambda r: r.replace("90Y1234", "SN-X"))
    y_live = await _drive_osa(recorder)
    transcript = recorder.save(tmp_path / "aq6370d_acquire.jsonl")
    assert "90Y1234" not in transcript.read_text(encoding="utf-8")   # redacted

    replay = TranscriptReplayTransport(transcript)
    y_replayed = await _drive_osa(replay)
    assert replay.exhausted, "replay must consume the entire transcript"
    np.testing.assert_allclose(y_replayed, y_live)


async def test_rto6_record_then_replay_roundtrip(tmp_path):
    payload = np.linspace(-1, 1, 64, dtype="<f4").tobytes()
    rules = {"*IDN?": "ROHDE&SCHWARZ,RTO6,1329.7002k44,5.35.2.0",
             "CHANnel1:DATA:HEADer?": "0.0,1e-3,64,1"}

    async def drive(transport) -> np.ndarray:
        driver = RTO6Driver(transport)
        await driver.initialize()
        assert "RTO6" in await driver.identify()
        header = await driver.read_waveform_header(1)
        assert header[2] == 64
        return await driver.read_waveform(1)

    recorder = RecordingScpiTransport(MockScpiTransport(
        rules, binary_rules={"CHANnel1:DATA?": payload}))
    y_live = await drive(recorder)
    transcript = recorder.save(tmp_path / "rto6_waveform.jsonl")

    replay = TranscriptReplayTransport(transcript)
    y_replayed = await drive(replay)
    assert replay.exhausted
    np.testing.assert_allclose(y_replayed, y_live)


async def test_replay_fails_on_command_deviation(tmp_path):
    """A driver change that alters the command stream must fail the L2 test."""
    recorder = RecordingScpiTransport(MockScpiTransport({"*IDN?": "X,Y,1,2"}))
    await recorder.query("*IDN?")
    transcript = recorder.save(tmp_path / "tiny.jsonl")

    replay = TranscriptReplayTransport(transcript)
    with pytest.raises(InstrumentProtocolError, match="mismatch"):
        await replay.query("*OPC?")            # deviates from the recording

    replay2 = TranscriptReplayTransport(transcript)
    await replay2.query("*IDN?")
    with pytest.raises(InstrumentProtocolError, match="exhausted"):
        await replay2.query("*IDN?")           # transcript over-consumed
