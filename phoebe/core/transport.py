"""Transport protocol: the driver's swappable dependency (refactor.md §4.2).

The ``async`` methods do NOT imply the underlying I/O is non-blocking — the
blocking call is encapsulated in the device's worker thread (§12.3); the
transport merely posts the job and awaits its future.

Binary block helpers live here because both VISA and raw-TCP SCPI devices
speak IEEE 488.2 definite-length blocks for waveform payloads.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .errors import InstrumentProtocolError


@runtime_checkable
class ScpiTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, command: str) -> None: ...
    async def query(self, command: str) -> str: ...

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        """Send ``<prefix><#-block><payload>`` as one message (waveform upload)."""
        ...

    async def query_binary(self, command: str) -> bytes:
        """Query returning an IEEE 488.2 block; resolves to the raw payload bytes."""
        ...


def make_ieee_block(payload: bytes) -> bytes:
    """IEEE 488.2 definite-length block header + payload."""
    length = str(len(payload)).encode("ascii")
    return b"#" + str(len(length)).encode("ascii") + length + payload


def parse_ieee_block(raw: bytes) -> bytes:
    """Extract the payload from an IEEE 488.2 definite-length block reply."""
    if not raw:
        raise InstrumentProtocolError("empty binary reply")
    start = raw.find(b"#")
    if start < 0:
        raise InstrumentProtocolError(f"no IEEE block header in reply: {raw[:32]!r}")
    try:
        ndigits = int(raw[start + 1:start + 2])
        length = int(raw[start + 2:start + 2 + ndigits])
    except (ValueError, IndexError) as exc:
        raise InstrumentProtocolError(f"bad IEEE block header: {raw[:32]!r}") from exc
    data_start = start + 2 + ndigits
    payload = raw[data_start:data_start + length]
    if len(payload) != length:
        raise InstrumentProtocolError(
            f"IEEE block truncated: expected {length} bytes, got {len(payload)}"
        )
    return payload
