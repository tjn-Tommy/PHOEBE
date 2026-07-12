"""Recording transport: capture an L2 transcript from a live session
(refactor.md §14.1; plan B-5).

Wrap the real transport at the composition point, run the workflow once
against real hardware, then ``save()`` — the JSONL output replays through
``TranscriptReplayTransport`` (transports/mock.py) as an offline regression
test that fails on any deviation from the recorded command stream.

``redact`` scrubs identifying material (serial numbers, IPs) from replies
*before* a transcript is committed to the repo; commands are recorded
verbatim because they are the very thing under test.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..core.transport import ScpiTransport


class RecordingScpiTransport:
    """Pass-through wrapper that records every exchange as replayable JSONL."""

    def __init__(self, inner: ScpiTransport, *,
                 redact: Callable[[str], str] | None = None) -> None:
        self._inner = inner
        self._redact = redact or (lambda reply: reply)
        self.records: list[dict] = []

    # ---- ScpiTransport surface ------------------------------------------------
    async def open(self) -> None:
        await self._inner.open()

    async def close(self) -> None:
        await self._inner.close()

    async def write(self, command: str) -> None:
        await self._inner.write(command)
        self.records.append({"op": "write", "command": command})

    async def query(self, command: str) -> str:
        reply = await self._inner.query(command)
        self.records.append({"op": "query", "command": command,
                             "reply": self._redact(reply)})
        return reply

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        await self._inner.write_binary(command_prefix, payload)
        self.records.append({"op": "write_binary", "command": command_prefix,
                             "payload_len": len(payload)})

    async def query_binary(self, command: str) -> bytes:
        reply = await self._inner.query_binary(command)
        self.records.append({"op": "query_binary", "command": command,
                             "reply_hex": reply.hex()})
        return reply

    # ---- transcript output ----------------------------------------------------
    def save(self, path: Path | str) -> Path:
        """Write the transcript; one JSON object per line, replay-compatible."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record, ensure_ascii=True) for record in self.records]
        p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return p
