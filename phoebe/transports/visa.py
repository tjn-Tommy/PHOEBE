"""PyVISA-backed SCPI transport (imports pyvisa lazily; real backend only).

Blocking VISA calls run on the device's worker thread (refactor.md §12.3).
"""
from __future__ import annotations

from ..core.errors import (
    InstrumentConnectionError,
    InstrumentTimeoutError,
)
from ..core.transport import make_ieee_block, parse_ieee_block
from ..core.worker import BlockingDeviceWorker


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
            raw = inst.read_raw()
        except Exception as exc:
            raise self._map_exc(exc, repr(command)) from exc
        return parse_ieee_block(raw)
