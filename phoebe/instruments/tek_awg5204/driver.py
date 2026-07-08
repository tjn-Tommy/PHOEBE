"""AWG5204 driver: vendor command set over an injected ScpiTransport
(migrated from awg5204/awg5204_tm/driver.py).

The original driver spoke to the instrument through the ``tm_devices`` command
tree (``self._awg.commands.<...>.write()``).  This port drops that dependency
entirely: every command-tree call is translated into the equivalent raw SCPI
string sent over the platform's :class:`ScpiTransport` (which routes the
blocking VISA I/O onto the device worker thread).  The driver owns SCPI
formatting only; lifecycle, locking and domain translation live in the
controller.
"""
from __future__ import annotations

import numpy as np

from ...core.errors import InstrumentProtocolError
from ...core.transport import ScpiTransport


class AWG5204Driver:
    """Deploy-free SCPI surface for a Tektronix AWG5204 (AWG5200 series).

    Each method maps one-to-one onto the tm_devices command-tree call it
    replaces; orchestration (delete-then-upload, sequencing, run control under
    a lock) belongs to :class:`AWG5204Controller`.
    """

    def __init__(self, transport: ScpiTransport) -> None:
        self._t = transport

    # --- identification / status ---------------------------------------------
    async def identify(self) -> str:
        """``idn.query()`` -> ``*IDN?``."""
        return await self._t.query("*IDN?")

    async def reset(self) -> None:
        """``rst.write()`` -> ``*RST``."""
        await self._t.write("*RST")

    async def clear_status(self) -> None:
        """``cls.write()`` -> ``*CLS``."""
        await self._t.write("*CLS")

    async def query_error(self) -> str:
        """Read one entry from the SCPI error queue (``SYSTem:ERRor?``)."""
        return await self._t.query("SYSTem:ERRor?")

    async def query_run_state(self) -> int:
        """Run state: 0 stopped, 1 waiting-for-trigger, 2 running (``AWGControl:RSTate?``)."""
        reply = await self._t.query("AWGControl:RSTate?")
        try:
            return int(float(reply))
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"bad run-state reply {reply!r}") from exc

    # --- output configuration -------------------------------------------------
    async def set_output_state(self, ch: int, on: bool) -> None:
        """``outputx[ch].state.write(v)`` -> ``OUTPut<ch>:STATe <0|1>``."""
        await self._t.write(f"OUTPut{ch}:STATe {1 if on else 0}")

    async def set_amplitude(self, ch: int, vpp: float) -> None:
        """``sourcex[ch].voltage.level.immediate.amplitude.write(a)``."""
        await self._t.write(
            f"SOURce{ch}:VOLTage:LEVel:IMMediate:AMPLitude {vpp}")

    async def set_offset(self, ch: int, v: float) -> None:
        """``sourcex[ch].voltage.level.immediate.offset.write(o)``."""
        await self._t.write(
            f"SOURce{ch}:VOLTage:LEVel:IMMediate:OFFSet {v}")

    async def configure_marker(self, marker) -> None:
        """Marker delay + voltage levels (``sourcex[ch].marker[m]...``).

        Applies whichever voltage fields are provided; HIGH/LOW are written
        last so they take precedence over AMPLitude/OFFSet, matching the note
        in the original driver.
        """
        ch = marker.channel
        m = marker.marker
        await self._t.write(f"SOURce{ch}:MARKer{m}:DELay {marker.delay_s}")
        if marker.amplitude_v is not None:
            await self._t.write(
                f"SOURce{ch}:MARKer{m}:VOLTage:LEVel:IMMediate:AMPLitude "
                f"{marker.amplitude_v}")
        if marker.offset_v is not None:
            await self._t.write(
                f"SOURce{ch}:MARKer{m}:VOLTage:LEVel:IMMediate:OFFSet "
                f"{marker.offset_v}")
        if marker.high_level_v is not None:
            await self._t.write(
                f"SOURce{ch}:MARKer{m}:VOLTage:LEVel:IMMediate:HIGH "
                f"{marker.high_level_v}")
        if marker.low_level_v is not None:
            await self._t.write(
                f"SOURce{ch}:MARKer{m}:VOLTage:LEVel:IMMediate:LOW "
                f"{marker.low_level_v}")

    async def set_run_mode(self, mode: str) -> None:
        """``awgcontrol.rmode.write(m)`` -> ``AWGControl:RMODe <mode>``."""
        await self._t.write(f"AWGControl:RMODe {mode}")

    async def configure_trigger(self, trigger) -> None:
        """Trigger source (always) plus any non-None slope/level/impedance/interval."""
        await self._t.write(f"TRIGger:SOURce {trigger.source}")
        if trigger.slope is not None:
            await self._t.write(f"TRIGger:SLOPe {trigger.slope}")
        if trigger.level_v is not None:
            await self._t.write(f"TRIGger:LEVel {trigger.level_v}")
        if trigger.impedance_ohm is not None:
            await self._t.write(f"TRIGger:IMPedance {trigger.impedance_ohm}")
        if trigger.interval_s is not None:
            await self._t.write(f"TRIGger:INTerval {trigger.interval_s}")

    async def set_reference_clock(self, source: str,
                                  multiplier: float | None = None) -> None:
        """``clock.source.write(s)`` plus optional external ROSC multiplier."""
        await self._t.write(f"CLOCk:SOURce {source}")
        if source.upper() == "EXT" and multiplier is not None:
            await self._t.write(f"SOURce:ROSCillator:MULTiplier {multiplier}")

    # --- run control ----------------------------------------------------------
    async def force_trigger(self) -> None:
        """``trigger.immediate.write("ATR")`` -> ``TRIGger:IMMediate ATR``."""
        await self._t.write("TRIGger:IMMediate ATR")

    async def run(self) -> None:
        """``awgcontrol.run.immediate.write()`` -> ``AWGControl:RUN:IMMediate``."""
        await self._t.write("AWGControl:RUN:IMMediate")

    async def stop(self) -> None:
        """``awgcontrol.stop.immediate.write()`` -> ``AWGControl:STOP:IMMediate``."""
        await self._t.write("AWGControl:STOP:IMMediate")

    # --- sequence / waveform management ---------------------------------------
    async def delete_sequence(self, name: str) -> None:
        """``slist.sequence.delete.write('"name"')``.

        Deleting a non-existent sequence errors on the device; the controller
        wraps best-effort cleanup in try/except.
        """
        await self._t.write(f'SLISt:SEQuence:DELete "{name}"')

    async def delete_waveform(self, name: str) -> None:
        """``wlist.waveform.delete.write('"name"')`` (errors if absent — see above)."""
        await self._t.write(f'WLISt:WAVeform:DELete "{name}"')

    async def new_waveform(self, name: str, length: int) -> None:
        """``wlist.waveform.new.write('"name", len, REAL')``."""
        await self._t.write(f'WLISt:WAVeform:NEW "{name}", {length}, REAL')

    async def upload_waveform_samples(self, name: str, samples: np.ndarray) -> None:
        """Upload analog samples as float32 little-endian IEEE 488.2 block data."""
        data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
        n = int(np.asarray(samples).size)
        await self._t.write_binary(
            f'WLISt:WAVeform:DATA "{name}",0,{n},', data)

    async def upload_marker_data(self, name: str, bits: np.ndarray) -> None:
        """Upload packed marker bits as uint8 IEEE 488.2 block data."""
        data = np.ascontiguousarray(bits, dtype=np.uint8).tobytes()
        n = int(np.asarray(bits).size)
        await self._t.write_binary(
            f'WLISt:WAVeform:MARKer:DATA "{name}",0,{n},', data)

    async def new_sequence(self, name: str, steps: int) -> None:
        """``slist.sequence.new.write('"name", steps')``."""
        await self._t.write(f'SLISt:SEQuence:NEW "{name}", {steps}')

    async def assign_step_waveform(self, seq: str, step_index: int, track: int,
                                   waveform_name: str) -> None:
        """``slist.sequence.stepx[i].tassetx[t].waveform.write('"seq", "wfm"')``."""
        await self._t.write(
            f'SLISt:SEQuence:STEP{step_index}:TASSet{track}:WAVeform '
            f'"{seq}", "{waveform_name}"')

    async def set_step_repeat(self, seq: str, step_index: int,
                              repeat_token: str) -> None:
        """``slist.sequence.stepx[i].rcount.write('"seq", r')`` (r is int or ``INF``)."""
        await self._t.write(
            f'SLISt:SEQuence:STEP{step_index}:RCOunt "{seq}", {repeat_token}')

    async def set_clock_sample_rate(self, rate: float) -> None:
        """``clock.srate.write(rate)`` -> ``CLOCk:SRATe <rate>``."""
        await self._t.write(f"CLOCk:SRATe {rate:.12g}")

    async def set_sequence_sample_rate(self, seq: str, rate: float) -> None:
        """``slist.sequence.srate.write('"seq", rate')``."""
        await self._t.write(f'SLISt:SEQuence:SRATe "{seq}", {rate:.12g}')

    async def assign_sequence_to_channel(self, ch: int, seq: str,
                                         track: int) -> None:
        """``sourcex[ch].casset.sequence.write('"seq", track')``."""
        await self._t.write(f'SOURce{ch}:CASSet:SEQuence "{seq}", {track}')
