"""Raw-TCP SCPI transport (line-oriented ASCII, CR/LF terminated).

All socket calls run on the device's worker thread; the async methods post
jobs and await futures (refactor.md §12.3).  Vendor-specific login handshakes
(e.g. the AQ637X telnet-style auth) are injected as an ``on_open`` hook that
receives a small raw-I/O facade — the transport stays generic.
"""
from __future__ import annotations

import socket
from collections.abc import Callable

from ..core.errors import (
    InstrumentConnectionError,
    InstrumentProtocolError,
    InstrumentTimeoutError,
)
from ..core.transport import parse_ieee_block
from ..core.worker import BlockingDeviceWorker


class RawTcpIo:
    """Blocking raw-I/O facade handed to on_open handshakes (worker thread only)."""

    def __init__(self, sock: socket.socket, buffer_size: int) -> None:
        self._sock = sock
        self._buffer_size = buffer_size

    def send_line(self, text: str) -> None:
        self._sock.sendall(f"{text}\r\n".encode("ascii"))

    def recv_once(self) -> str:
        """Read whatever is currently available (prompts without newline)."""
        chunk = self._sock.recv(self._buffer_size)
        if not chunk:
            raise InstrumentConnectionError("connection closed during handshake")
        return chunk.decode("ascii", errors="replace").strip()


class TcpScpiTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        worker: BlockingDeviceWorker,
        timeout_s: float = 10.0,
        write_delay_s: float = 0.0,
        buffer_size: int = 8192,
        on_open: Callable[[RawTcpIo], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._worker = worker
        self._timeout = timeout_s
        self._write_delay = write_delay_s
        self._buffer_size = buffer_size
        self._on_open = on_open
        self._sock: socket.socket | None = None

    # ---- async surface (posts to the worker thread) --------------------------
    async def open(self) -> None:
        await self._worker.call(self._open_blocking)

    async def close(self) -> None:
        await self._worker.call(self._close_blocking)

    async def invalidate(self) -> None:
        """Drop the handle without protocol niceties — the reconnect hook for
        a link known to be dead; the next ``open()`` rebuilds it."""
        await self._worker.call(self._discard)

    async def write(self, command: str) -> None:
        await self._worker.call(self._write_blocking, command)

    async def query(self, command: str) -> str:
        return await self._worker.call(self._query_blocking, command)

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        await self._worker.call(self._write_binary_blocking, command_prefix, payload)

    async def query_binary(self, command: str) -> bytes:
        return await self._worker.call(self._query_binary_blocking, command)

    # ---- blocking implementations (worker thread only) -----------------------
    def _open_blocking(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect((self._host, self._port))
            self._sock = sock
            if self._on_open is not None:
                self._on_open(RawTcpIo(sock, self._buffer_size))
        except InstrumentConnectionError:
            self._discard()
            raise
        except TimeoutError as exc:
            self._discard()
            raise InstrumentTimeoutError(
                f"timed out connecting to {self._host}:{self._port}") from exc
        except OSError as exc:
            self._discard()
            raise InstrumentConnectionError(
                f"failed to connect to {self._host}:{self._port}: {exc}") from exc

    def _close_blocking(self) -> None:
        self._discard()

    def _discard(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise InstrumentConnectionError("socket is not open")
        return self._sock

    def _send(self, data: bytes, label: str) -> None:
        sock = self._require_sock()
        try:
            sock.sendall(data)
        except TimeoutError as exc:
            raise InstrumentTimeoutError(f"timed out sending {label}") from exc
        except OSError as exc:
            self._discard()                   # dead handle: next open() rebuilds
            raise InstrumentConnectionError(f"failed sending {label}: {exc}") from exc

    def _write_blocking(self, command: str) -> None:
        import time
        self._send(f"{command}\r\n".encode("ascii"), repr(command))
        if self._write_delay > 0:
            time.sleep(self._write_delay)

    def _recv_line(self) -> bytes:
        sock = self._require_sock()
        data = b""
        while not data.endswith(b"\n"):
            try:
                chunk = sock.recv(self._buffer_size)
            except TimeoutError as exc:
                raise InstrumentTimeoutError("timed out waiting for reply") from exc
            except OSError as exc:
                self._discard()
                raise InstrumentConnectionError(f"socket read error: {exc}") from exc
            if not chunk:
                self._discard()
                raise InstrumentConnectionError("connection closed by device")
            data += chunk
        return data

    def _recv_exact(self, n: int, label: str) -> bytes:
        """Read exactly ``n`` bytes; a peer close or timeout raises instead of
        busy-looping on empty recv() forever (H9)."""
        sock = self._require_sock()
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(min(self._buffer_size, n - len(buf)))
            except TimeoutError as exc:
                raise InstrumentTimeoutError(
                    f"timed out reading {label} ({len(buf)}/{n} bytes)") from exc
            except OSError as exc:
                self._discard()
                raise InstrumentConnectionError(
                    f"socket read error during {label}: {exc}") from exc
            if not chunk:
                self._discard()
                raise InstrumentConnectionError(
                    f"connection closed by device during {label} "
                    f"({len(buf)}/{n} bytes)")
            buf.extend(chunk)
        return bytes(buf)

    def _query_blocking(self, command: str) -> str:
        self._send(f"{command}\r\n".encode("ascii"), repr(command))
        return self._recv_line().decode("ascii", errors="replace").strip()

    def _write_binary_blocking(self, command_prefix: str, payload: bytes) -> None:
        length = str(len(payload)).encode("ascii")
        block = b"#" + str(len(length)).encode("ascii") + length + payload
        self._send(command_prefix.encode("ascii") + block + b"\n",
                   repr(command_prefix))

    def _query_binary_blocking(self, command: str) -> bytes:
        self._send(f"{command}\r\n".encode("ascii"), repr(command))
        # read the '#' + digit-count + length header, then exactly length bytes;
        # every read detects peer close and maps timeouts (H9)
        header = self._recv_exact(2, "IEEE block header")
        if not header.startswith(b"#"):
            # not a block: fall back to line read and let the parser complain
            rest = self._recv_line()
            return parse_ieee_block(header + rest)
        ndigits = int(header[1:2])
        if ndigits == 0:
            raise InstrumentProtocolError(
                "indefinite-length IEEE block (#0) is not supported")
        length_bytes = self._recv_exact(ndigits, "IEEE block length")
        try:
            length = int(length_bytes)
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"bad IEEE block length {length_bytes!r}") from exc
        payload = self._recv_exact(length, "IEEE block payload") if length else b""
        # consume the trailing newline if present
        sock = self._require_sock()
        try:
            sock.settimeout(0.1)
            sock.recv(2)
        except OSError:
            pass
        finally:
            sock.settimeout(self._timeout)
        return payload
