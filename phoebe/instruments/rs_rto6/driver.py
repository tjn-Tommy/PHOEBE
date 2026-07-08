"""RTO6 driver: vendor command set over an injected ScpiTransport (async).

Migrated from ``TPA_experiment/src/scope_module/driver/driver.py``
(``RTO6_Driver`` over PyVISA / HiSLIP) and ``base_scope.py``.

The driver owns SCPI formatting and reply parsing only; lifecycle, locking,
polling and domain translation live in the controller.  Waveforms are
downloaded as IEEE-488.2 binary blocks (``FORMat REAL,32``) for speed.

SCPI note: every command string used here is verified against the RTO6 SCPI
manual; manual references are preserved from the original driver.
"""
from __future__ import annotations

import numpy as np

from ...core.errors import InstrumentProtocolError
from ...core.transport import ScpiTransport

# Bit 0 of the standard event status register (*ESR?) is the Operation Complete
# bit, set once a "SINGle;*OPC" acquisition has finished.
_OPC_BIT = 0x01


class RTO6Driver:
    """Rohde & Schwarz RTO6-series oscilloscope command set.

    Mirrors ``AQ637XDriver`` in shape: the injected transport carries the wire
    protocol, every public method is a thin async SCPI wrapper, and the vendor
    command set lives here.
    """

    # Source token -> the LEVel<n> index (RTO6: 1..4 channels, 5 = ext input).
    _TRIG_LEVEL_INDEX = {
        "CHANNEL1": 1, "CHAN1": 1, "C1": 1,
        "CHANNEL2": 2, "CHAN2": 2, "C2": 2,
        "CHANNEL3": 3, "CHAN3": 3, "C3": 3,
        "CHANNEL4": 4, "CHAN4": 4, "C4": 4,
        "EXTERNANALOG": 5, "EXT": 5,
    }

    def __init__(self, transport: ScpiTransport) -> None:
        self._t = transport

    async def initialize(self) -> None:
        """Put the scope into the known binary-transfer state used everywhere.

        Previously done inline in ``_open_transport``.
        """
        await self._t.write("*CLS")                    # clear status registers
        await self._t.write("FORMat:DATA REAL,32")     # 32-bit float waveform samples
        await self._t.write("FORMat:BORDer LSBFirst")  # little-endian binary blocks
        # CHANnel:DATA? honours INCXvalues; keep it OFF so we get Y-only and
        # build the time axis from the header (RTO6 manual 27.8.6).
        await self._t.write("EXPort:WAVeform:INCXvalues OFF")

    # --- identification ---------------------------------------------------
    async def identify(self) -> str:
        return await self._t.query("*IDN?")

    # --- channel configuration -------------------------------------------
    async def configure_channel(
        self,
        channel: int,
        *,
        state: bool = True,
        scale: float | str | None = None,
        offset: float | str | None = None,
        coupling: str | None = None,
    ) -> None:
        """Enable a channel and set its vertical parameters (only those given).

        ``scale`` is volts/division, ``offset`` is volts, ``coupling`` is the
        RTO coupling token (e.g. DC, DCLimit, AC).
        """
        ch = int(channel)
        await self._t.write(f"CHANnel{ch}:STATe {'ON' if state else 'OFF'}")
        if scale is not None:
            await self._t.write(f"CHANnel{ch}:SCALe {scale}")          # V/div
        if offset is not None:
            await self._t.write(f"CHANnel{ch}:OFFSet {offset}")        # V
        if coupling is not None:
            await self._t.write(f"CHANnel{ch}:COUPling {coupling}")    # DC | DCLimit | AC

    async def set_decimation(self, channel: int, mode: str) -> None:
        """Set the per-channel decimation / waveform type.

        SAMPle keeps raw samples; PDETect (peak detect) keeps the min/max of
        each decimation interval (two values per sample), so reduced records
        still capture true pulse peaks; HRESolution boxcar-averages.
        """
        # RTO6 decimation: CHANnel<n>:TYPE {SAMPle|PDETect|HRESolution|RMS}
        await self._t.write(f"CHANnel{int(channel)}:TYPE {mode}")

    async def set_arithmetics(self, channel: int, mode: str) -> None:
        """Set how consecutive acquisitions are combined.

        OFF gives raw single-shot data; AVERage means across-acquisition
        averaging; ENVelope accumulates min/max. Kept explicit so a capture is
        deterministically raw regardless of the scope's prior front-panel state.
        """
        # RTO6: CHANnel<n>:ARIThmetics {OFF|AVERage|ENVelope}
        await self._t.write(f"CHANnel{int(channel)}:ARIThmetics {mode}")

    async def set_bandwidth_limit(self, channel: int, limit: str) -> None:
        """Limit the channel's analog bandwidth to cut wideband noise.

        RTO6: CHANnel<n>:BANDwidth {FULL|B800|B200|B20}. B20 (20 MHz) is the
        lowest hardware limit -- a large SNR win for a low-frequency signal.
        """
        await self._t.write(f"CHANnel{int(channel)}:BANDwidth {limit}")

    async def set_digital_filter(self, channel: int, cutoff: float | None) -> None:
        """Enable/disable the per-channel digital low-pass filter.

        RTO6: CHANnel<n>:DIGFilter:STATe / :CUToff <Hz>. Passing None turns the
        filter off. For a smooth <=kHz signal a cutoff ~10x the signal bandwidth
        rejects almost all noise.
        """
        ch = int(channel)
        if cutoff is None:
            await self._t.write(f"CHANnel{ch}:DIGFilter:STATe OFF")
        else:
            await self._t.write(f"CHANnel{ch}:DIGFilter:STATe ON")
            await self._t.write(f"CHANnel{ch}:DIGFilter:CUToff {cutoff}")

    # --- trigger ----------------------------------------------------------
    async def set_trigger_mode(self, mode: str) -> None:
        """Set only the trigger mode (AUTO free-runs, NORMal waits for an edge).

        Used for the software/immediate read: MODE AUTO with no armed edge lets
        SINGle self-trigger and complete, matching the proven free-run capture.
        """
        await self._t.write(f"TRIGger1:MODE {mode}")

    async def set_trigger(
        self,
        *,
        source: str = "CHANnel1",
        level: float | None = None,
        slope: str = "POSitive",
        mode: str = "NORMal",
    ) -> None:
        """Configure a single edge trigger (A-event).

        mode: AUTO | NORMal | FREerun (NORMal waits for a real trigger).
        source: CHANnel1..4 or EXTernanalog. slope: POSitive (rising) etc.
        The level is written to the LEVel index that matches the source.
        """
        await self._t.write(f"TRIGger1:MODE {mode}")
        await self._t.write(f"TRIGger1:SOURce {source}")
        await self._t.write("TRIGger1:TYPE EDGE")
        await self._t.write(f"TRIGger1:EDGE:SLOPe {slope}")
        if level is not None:
            n = self._TRIG_LEVEL_INDEX.get(source.upper(), 1)
            await self._t.write(f"TRIGger1:LEVel{n} {level}")

    # --- acquisition / horizontal ----------------------------------------
    async def set_time_range(self, seconds: str | float) -> None:
        """Set the full acquisition time window (TIMebase:RANGe, in seconds)."""
        await self._t.write(f"TIMebase:RANGe {seconds}")

    async def set_acquisition_count(self, count: int = 1) -> None:
        """Set how many acquisitions one SINGle runs (ACQuire:COUNt).

        Must be 1 for a single-shot capture: a stale count (left high by a prior
        averaging run) makes SINGle acquire many records, so the OPC-complete bit
        never latches within the poll timeout and the read wrongly times out.
        """
        await self._t.write(f"ACQuire:COUNt {int(count)}")

    async def set_record_length(self, points: int | None) -> None:
        """Fix the record length, or let the scope keep resolution constant.

        ACQuire:POINts:AUTO selects *which* quantity stays constant when the
        time range changes: RECLength pins the record length (so resolution
        adapts -- what we want for a fixed point budget over 1 s), RESolution
        hands record length back to the scope.
        """
        if points is None:
            await self._t.write("ACQuire:POINts:AUTO RESolution")
        else:
            await self._t.write("ACQuire:POINts:AUTO RECLength")
            await self._t.write(f"ACQuire:POINts {int(points)}")

    async def set_post_trigger_window(self) -> None:
        """Place the trigger at the record's left edge (all data post-trigger).

        REFerence 0 puts the reference point at 0% of the screen and
        HORizontal:POSition 0 makes it coincide with the trigger (the zero
        point), so the acquisition spans [0, TIMebase:RANGe] after the trigger.
        """
        await self._t.write("TIMebase:REFerence 0")
        await self._t.write("TIMebase:HORizontal:POSition 0")

    async def sample_rate(self) -> float:
        return float(await self._t.query("ACQuire:SRATe?"))

    async def record_length(self) -> int:
        return int(float(await self._t.query("ACQuire:POINts?")))

    # --- single-acquisition control --------------------------------------
    async def stop(self) -> None:
        await self._t.write("STOP")

    async def single_acquisition(self) -> None:
        """Arm one acquisition and flag operation-complete for polling.

        Uses the classic pollable-OPC handshake: stop any running acquisition
        for a clean start, clear the status registers, then ``SINGle;*OPC`` so
        that *ESR? bit 0 latches when this one acquisition finishes -- this
        stays abortable, unlike a blocking ``*OPC?``.
        """
        await self._t.write("STOP")
        await self._t.write("*CLS")
        await self._t.write("SINGle;*OPC")

    async def event_status(self) -> int:
        """Read (and clear) the standard event status register (*ESR?)."""
        reply = await self._t.query("*ESR?")
        try:
            return int(float(reply))
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"bad event-status reply {reply!r}") from exc

    async def is_acquisition_complete(self) -> bool:
        return bool(await self.event_status() & _OPC_BIT)

    # --- waveform download ------------------------------------------------
    async def read_waveform_header(self, channel: int) -> tuple[float, float, int, int]:
        """Return (x_start_s, x_stop_s, record_length, values_per_sample).

        RTO6 returns "XStart,XStop,RecordLength,ValuesPerSample" -- values per
        sample is 2 for peak-detect/envelope waveforms, 1 otherwise.
        """
        raw = await self._t.query(f"CHANnel{int(channel)}:DATA:HEADer?")
        parts = [p for p in raw.replace(";", ",").split(",") if p != ""]
        if len(parts) < 4:
            raise InstrumentProtocolError(f"unexpected waveform header: {raw!r}")
        try:
            x_start = float(parts[0])
            x_stop = float(parts[1])
            record_length = int(float(parts[2]))
            values_per_sample = int(float(parts[3]))
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"unparseable waveform header: {raw!r}") from exc
        return x_start, x_stop, record_length, values_per_sample

    async def read_waveform(self, channel: int) -> np.ndarray:
        """Download the raw Y values of a channel as a float array.

        CHANnel<n>:DATA? is the [:WAVeform1] shorthand; INCXvalues is forced OFF
        at initialize() so only Y-values come back.  ``query_binary`` returns the
        raw IEEE-block payload; FORMat REAL,32 + BORDer LSBFirst make it a
        little-endian float32 stream.
        """
        payload = await self._t.query_binary(f"CHANnel{int(channel)}:DATA?")
        return np.frombuffer(payload, dtype="<f4").astype(np.float64)

    # --- automatic measurement (on-scope averaging over the record) -------
    async def _setup_amptime_measurement(
        self,
        channel: int,
        main: str,
        *,
        group: int,
        gate_start: float | None = None,
        gate_stop: float | None = None,
    ) -> None:
        """Configure measurement group <group> for ch<channel> <main> (AMPTime).

        <main> is an amplitude/time measurement type (MEAN, STDDev, ...); the
        scope computes it over the waveform window for us, so we read one scalar
        instead of transferring the whole record. An optional absolute time gate
        [gate_start, gate_stop] (seconds after the trigger) restricts it to the
        settled part of the acquisition.
        """
        mg = int(group)
        await self._t.write(f"MEASurement{mg}:ENABle ON")
        await self._t.write(f"MEASurement{mg}:CATegory AMPTime")
        await self._t.write(f"MEASurement{mg}:SOURce C{int(channel)}W1")
        await self._t.write(f"MEASurement{mg}:MAIN {main}")
        if gate_start is not None and gate_stop is not None:
            await self._t.write(f"MEASurement{mg}:GATE:MODE ABS")
            await self._t.write(f"MEASurement{mg}:GATE:ABSolute:STARt {gate_start}")
            await self._t.write(f"MEASurement{mg}:GATE:ABSolute:STOP {gate_stop}")
            await self._t.write(f"MEASurement{mg}:GATE:STATe ON")
        else:
            await self._t.write(f"MEASurement{mg}:GATE:STATe OFF")

    async def setup_mean_measurement(
        self,
        channel: int,
        *,
        group: int = 1,
        gate_start: float | None = None,
        gate_stop: float | None = None,
    ) -> None:
        """Configure measurement group <group> to return ch<channel> MEAN.

        The MEAN measurement is the average of the waveform over its (gated)
        window -- the scope computes the averaged signal level for us, so we read
        one scalar instead of transferring the whole record.
        """
        await self._setup_amptime_measurement(
            channel, "MEAN", group=group, gate_start=gate_start, gate_stop=gate_stop
        )

    async def setup_stddev_measurement(
        self,
        channel: int,
        *,
        group: int = 2,
        gate_start: float | None = None,
        gate_stop: float | None = None,
    ) -> None:
        """Configure measurement group <group> to return ch<channel> STDDev.

        STDDev is the standard deviation of the waveform samples over the (gated)
        window -- the within-shot spread/noise of the signal, read as one scalar
        alongside the MEAN. Uses the same absolute time gate so it covers the
        identical settled window the mean is averaged over.
        """
        await self._setup_amptime_measurement(
            channel, "STDDev", group=group, gate_start=gate_start, gate_stop=gate_stop
        )

    async def read_measurement(self, group: int = 1) -> float:
        """Return the current main-measurement result of a group as a float."""
        reply = await self._t.query(f"MEASurement{int(group)}:RESult:ACTual?")
        try:
            return float(reply)
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"bad measurement reply {reply!r}") from exc
