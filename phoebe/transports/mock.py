"""Test doubles for the transport layer (refactor.md §14.1).

* ``MockScpiTransport`` — L1 unit tests: scripted question/answer pairs verify
  the driver's command formatting and reply parsing.
* ``TranscriptReplayTransport`` — L2 regression: replays a recorded real-device
  session and fails on any deviation from the transcript.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from ..core.errors import InstrumentProtocolError


class MockScpiTransport:
    """Scripted transport: exact or fnmatch-pattern rules → canned replies.

    Every write/query is recorded in ``log`` so tests can assert the exact
    command stream a driver produced.
    """

    def __init__(self, rules: dict[str, str] | None = None,
                 *, binary_rules: dict[str, bytes] | None = None,
                 default_reply: str | None = None) -> None:
        self.rules = dict(rules or {})
        self.binary_rules = dict(binary_rules or {})
        self.default_reply = default_reply
        self.log: list[tuple[str, str]] = []      # (op, command)
        self.binary_payloads: list[tuple[str, bytes]] = []
        self.opened = False
        self.closed = False

    def add_rule(self, pattern: str, reply: str) -> None:
        self.rules[pattern] = reply

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def write(self, command: str) -> None:
        self.log.append(("write", command))

    async def query(self, command: str) -> str:
        self.log.append(("query", command))
        if command in self.rules:
            return self.rules[command]
        for pattern, reply in self.rules.items():
            if fnmatch.fnmatch(command, pattern):
                return reply
        if self.default_reply is not None:
            return self.default_reply
        raise InstrumentProtocolError(f"MockScpiTransport: no rule for {command!r}")

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        self.log.append(("write_binary", command_prefix))
        self.binary_payloads.append((command_prefix, payload))

    async def query_binary(self, command: str) -> bytes:
        self.log.append(("query_binary", command))
        if command in self.binary_rules:
            return self.binary_rules[command]
        for pattern, reply in self.binary_rules.items():
            if fnmatch.fnmatch(command, pattern):
                return reply
        raise InstrumentProtocolError(f"MockScpiTransport: no binary rule for {command!r}")

    def commands(self, op: str | None = None) -> list[str]:
        return [c for o, c in self.log if op is None or o == op]


class TranscriptReplayTransport:
    """Replays a JSONL transcript of ``{"op": ..., "command": ..., "reply": ...}``
    records; any out-of-order or unexpected command fails the test."""

    def __init__(self, transcript_path: str | Path) -> None:
        self._records = [
            json.loads(line)
            for line in Path(transcript_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self._cursor = 0

    def _next(self, op: str, command: str) -> dict:
        if self._cursor >= len(self._records):
            raise InstrumentProtocolError(
                f"transcript exhausted; unexpected {op} {command!r}")
        record = self._records[self._cursor]
        if record["op"] != op or record["command"] != command:
            raise InstrumentProtocolError(
                f"transcript mismatch at #{self._cursor}: expected "
                f"{record['op']} {record['command']!r}, got {op} {command!r}")
        self._cursor += 1
        return record

    @property
    def exhausted(self) -> bool:
        return self._cursor == len(self._records)

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, command: str) -> None:
        self._next("write", command)

    async def query(self, command: str) -> str:
        return self._next("query", command)["reply"]

    async def write_binary(self, command_prefix: str, payload: bytes) -> None:
        self._next("write_binary", command_prefix)

    async def query_binary(self, command: str) -> bytes:
        record = self._next("query_binary", command)
        return bytes.fromhex(record["reply_hex"])
