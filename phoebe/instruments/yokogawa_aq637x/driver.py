"""AQ637X driver: vendor command set over an injected ScpiTransport
(migrated from TPA_experiment/src/osa_module/driver/driver.py).

The driver owns SCPI formatting and reply parsing only; lifecycle, locking,
settling and domain translation live in the controller.
"""
from __future__ import annotations

import numpy as np

from ...core.errors import InstrumentConnectionError, InstrumentProtocolError
from ...core.transport import ScpiTransport
from ...domain.spectrum import SpectrumScanConfig

# Bit 0 of :STATus:OPERation:EVENt? is set when a sweep finishes.
_SWEEP_DONE_BIT = 0x01

_SENSITIVITY_TOKENS = {
    "norm": "NORMAL",
    "mid": "MID",
    "high1": "HIGH1",
    "high2": "HIGH2",
    "high3": "HIGH3",
}


def aq637x_handshake(username: str = "anonymous", password: str = ""):
    """Telnet-style login used by the AQ637X remote interface.

    Returns an ``on_open`` hook for TcpScpiTransport: send ``open "<user>"``,
    then the password line, and expect a reply starting with "ready".
    """

    def handshake(io) -> None:
        io.send_line(f'open "{username}"')
        io.recv_once()                      # username prompt/echo, discarded
        io.send_line(password)
        reply = io.recv_once()
        if not reply.lower().startswith("ready"):
            raise InstrumentConnectionError(
                f"OSA authentication failed; expected 'ready', got {reply!r}"
            )

    return handshake


class AQ637XDriver:
    def __init__(self, transport: ScpiTransport, *, command_format: int = 1) -> None:
        self._t = transport
        self._command_format = command_format

    async def initialize(self) -> None:
        """Select the AQ637X command format so documented SCPI applies."""
        await self._t.write(f"CFORM{self._command_format}")

    async def identify(self) -> str:
        return await self._t.query("*IDN?")

    # --- measurement configuration -------------------------------------------
    async def configure_scan(self, scan: SpectrumScanConfig) -> None:
        await self._t.write(f":SENSe:WAVelength:CENTer {scan.center_nm:.6f}NM")
        await self._t.write(f":SENSe:WAVelength:SPAN {scan.span_nm:.6f}NM")
        await self._t.write(f":SENSe:BANDwidth:RESolution {scan.resolution_nm:.4f}NM")
        await self._t.write(f":SENSe:SENSe {_SENSITIVITY_TOKENS[scan.sensitivity]}")
        await self._t.write(":SENSe:SWEEp:POINts:AUTO OFF")
        await self._t.write(f":SENSe:SWEEp:POINts {scan.points}")
        await self._t.write(":DISPlay:TRACe:Y:SPACing LOGarithmic")   # dBm axis
        await self._t.write(
            f":DISPlay:TRACe:Y:RLEVel {scan.reference_level_dbm:.2f}DBM")

    async def select_trace(self, trace_id: str, mode: str = "WRITe",
                           clear: bool = True) -> None:
        await self._t.write(f":TRACe:ACTive {trace_id}")
        await self._t.write(f":TRACe:ATTRibute:{trace_id} {mode}")
        if clear:
            await self._t.write(f":TRACe:CLEar {trace_id}")

    # --- sweep control ---------------------------------------------------------
    async def initiate_single_sweep(self) -> None:
        await self._t.write(":INITiate:SMODe 1")   # single-sweep mode
        await self._t.write("*CLS")                # clear status registers
        await self._t.write(":INITiate")           # start the sweep

    async def operation_event(self) -> int:
        """Read (and clear) the operation event register."""
        reply = await self._t.query(":STATus:OPERation:EVENt?")
        try:
            return int(reply)
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"bad operation-event reply {reply!r}") from exc

    async def sweep_complete(self) -> bool:
        return bool(await self.operation_event() & _SWEEP_DONE_BIT)

    async def abort(self) -> None:
        await self._t.write(":ABORt")

    # --- trace download ----------------------------------------------------------
    async def read_trace_x_nm(self, trace_id: str) -> np.ndarray:
        raw = await self._t.query(f":TRACe:X? {trace_id}")
        return _parse_block(raw) * 1e9             # device returns meters

    async def read_trace_y_dbm(self, trace_id: str) -> np.ndarray:
        raw = await self._t.query(f":TRACe:Y? {trace_id}")
        return _parse_block(raw)

    # --- snapshot queries ----------------------------------------------------------
    async def query_center_nm(self) -> float:
        return float(await self._t.query(":SENSe:WAVelength:CENTer?")) * 1e9

    async def query_span_nm(self) -> float:
        return float(await self._t.query(":SENSe:WAVelength:SPAN?")) * 1e9

    async def query_points(self) -> int:
        return int(float(await self._t.query(":SENSe:SWEEp:POINts?")))


def _parse_block(raw: str, drop_count_header: bool = True) -> np.ndarray:
    """Parse a comma-separated ASCII block; the first field is a point count."""
    if not raw:
        return np.empty(0, dtype=float)
    values = [float(v) for v in raw.split(",") if v != ""]
    if drop_count_header and values:
        values = values[1:]
    return np.asarray(values, dtype=float)
