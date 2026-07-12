"""PyVISA-backed SCPI transport (imports pyvisa lazily; real backend only).

Blocking VISA calls run on the device's worker thread (refactor.md §12.3).

Binary downloads use ``read_bytes()`` field by field instead of ``read_raw()``:
with ``read_termination="\\n"`` several backends stop a raw read at any 0x0A
byte, and float32 waveform payloads contain one every ~256 bytes (H8).
"""
from __future__ import annotations

from typing import Any

from ..core.errors import (
    InstrumentConnectionError,
    InstrumentProtocolError,
    InstrumentTimeoutError,
)
from ..core.transport import make_ieee_block
from ..core.worker import BlockingDeviceWorker


def read_ieee_block_bytes(inst: Any) -> bytes:
    """Read one IEEE 488.2 definite-length block with exact-count reads.

    ``inst`` is an open pyvisa MessageBasedResource (typed ``Any`` to keep the
    lazy-import rule).  Raises InstrumentProtocolError on malformed headers.
    """
    first = inst.read_bytes(1)
    while first in (b"\n", b"\r", b" "):        # stale terminator from a prior reply
        first = inst.read_bytes(1)
    if first != b"#":
        raise InstrumentProtocolError(f"no IEEE block header in reply: {first!r}")
    ndigits_raw = inst.read_bytes(1)
    if not ndigits_raw.isdigit():
        raise InstrumentProtocolError(f"bad IEEE block digit count: {ndigits_raw!r}")
    ndigits = int(ndigits_raw)
    if ndigits == 0:
        raise InstrumentProtocolError(
            "indefinite-length IEEE block (#0) is not supported")
    length_raw = inst.read_bytes(ndigits)
    try:
        length = int(length_raw)
    except ValueError as exc:
        raise InstrumentProtocolError(f"bad IEEE block length {length_raw!r}") from exc
    payload = inst.read_bytes(length) if length else b""
    # consume the trailing terminator so the next plain query starts clean;
    # bounded by a short timeout in case the device sends nothing more
    old_timeout = inst.timeout
    try:
        inst.timeout = 100                       # ms
        inst.read_bytes(1)
    except Exception:
        pass
    finally:
        inst.timeout = old_timeout
    return payload


class VisaScpiTransport:
    def __init__(
        self,
        resource_name: str,
        *,
        worker: BlockingDeviceWorker,
        timeout_s: float = 10.0,
        visa_library: str = "",
        chunk_size: int = 1 << 20,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None:
        self._resource_name = resource_name
        self._worker = worker
        self._timeout = timeout_s
        self._visa_library = visa_library
        self._chunk_size = chunk_size
        self._read_termination = read_termination
        self._write_termination = write_termination
        self._rm = None
        self._inst = None

    # ---- async surface --------------------------------------------------------
    async def open(self) -> None:
        await self._worker.call(self._open_blocking)

    async def close(self) -> None:
        await self._worker.call(self._close_blocking)

    async def write(self, command: str) -> None:
        await self._worker.call(self._write_blocking, command)

    async def query(self, command: str) -> str:
        return await self._worker.call(self._query_blocking, command)

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        await self._worker.call(self._write_binary_blocking, command_prefix, payload)

    async def query_binary(self, command: str) -> bytes:
        return await self._worker.call(self._query_binary_blocking, command)

    async def invalidate(self) -> None:
        """Drop the handle — the reconnect hook; the next ``open()`` rebuilds."""
        await self._worker.call(self._close_blocking)

    # ---- blocking implementations (worker thread only) -------------------------
    def _open_blocking(self) -> None:
        if self._inst is not None:
            return
        try:
            import pyvisa
        except ImportError as exc:
            raise InstrumentConnectionError(
                "pyvisa is required for VISA transports; "
                "install with `pip install pyvisa pyvisa-py`"
            ) from exc
        try:
            self._rm = pyvisa.ResourceManager(self._visa_library) \
                if self._visa_library else pyvisa.ResourceManager()
            inst = self._rm.open_resource(self._resource_name)
        except Exception as exc:
            self._close_blocking()
            raise InstrumentConnectionError(
                f"failed to open VISA resource {self._resource_name}: {exc}"
            ) from exc
        inst.timeout = self._timeout * 1000.0        # VISA timeout is in ms
        inst.chunk_size = self._chunk_size
        inst.read_termination = self._read_termination
        inst.write_termination = self._write_termination
        self._inst = inst

    def _close_blocking(self) -> None:
        for obj in (self._inst, self._rm):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._inst = None
        self._rm = None

    def _require(self):
        if self._inst is None:
            raise InstrumentConnectionError(
                f"VISA resource {self._resource_name} is not open")
        return self._inst

    def _map_exc(self, exc: Exception, label: str) -> Exception:
        try:
            import pyvisa.errors
            if isinstance(exc, pyvisa.errors.VisaIOError) and \
                    exc.error_code == pyvisa.errors.StatusCode.error_timeout:
                return InstrumentTimeoutError(f"timed out on {label}")
        except Exception:
            pass
        if "timeout" in str(exc).lower():
            return InstrumentTimeoutError(f"timed out on {label}")
        # connection-level trouble: drop the dead handle so a reconnect can
        # rebuild it instead of erroring on it forever
        self._close_blocking()
        return InstrumentConnectionError(f"VISA error on {label}: {exc}")

    def _write_blocking(self, command: str) -> None:
        try:
            self._require().write(command)
        except Exception as exc:
            raise self._map_exc(exc, repr(command)) from exc

    def _query_blocking(self, command: str) -> str:
        try:
            return self._require().query(command).strip()
        except Exception as exc:
            raise self._map_exc(exc, repr(command)) from exc

    def _write_binary_blocking(self, command_prefix: str, payload: bytes) -> None:
        inst = self._require()
        try:
            inst.write_raw(command_prefix.encode("ascii") + make_ieee_block(payload)
                           + self._write_termination.encode("ascii"))
        except Exception as exc:
            raise self._map_exc(exc, repr(command_prefix)) from exc

    def _query_binary_blocking(self, command: str) -> bytes:
        inst = self._require()
        try:
            inst.write(command)
            return read_ieee_block_bytes(inst)      # termchar-safe (H8)
        except InstrumentProtocolError:
            raise
        except Exception as exc:
            raise self._map_exc(exc, repr(command)) from exc
