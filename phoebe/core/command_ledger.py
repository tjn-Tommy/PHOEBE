"""CommandLedger: persisted command idempotency + audit (plan §6.4, PR C-3).

The ledger records every *accepted* dispatch (command_id, payload hash, the
first ack).  A resubmission with the same command_id:

* same payload  → the first ack is replayed (``AckCode.REPLAYED``) — a UI or
  network retry can never start a second run;
* different payload → ``COMMAND_ID_CONFLICT`` — a client bug is surfaced
  instead of silently executing something else under an old id.

Rejections are not recorded: they hold no resources and re-evaluating the
chain gives an equally correct (or better) answer.  SQLite persistence makes
the guarantee survive process restarts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from ..contracts.commands import AckCode, CommandAck, CommandEnvelope

LEDGER_FILENAME = "command_ledger.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    command_id   TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    command      TEXT NOT NULL,
    issued_by    TEXT NOT NULL,
    t_wall       TEXT NOT NULL,
    ack_json     TEXT NOT NULL
);
"""


def payload_hash(envelope: CommandEnvelope) -> str:
    """Canonical hash of (command, payload) — key order never matters."""
    canonical = json.dumps({"command": envelope.command,
                            "payload": envelope.payload},
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class CommandLedger:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def check(self, envelope: CommandEnvelope) -> CommandAck | None:
        """None → unseen command id (proceed).  A ``CommandAck`` → verdict:
        the replayed first ack (same payload) or a COMMAND_ID_CONFLICT
        rejection (different payload)."""
        row = self._db.execute(
            "SELECT payload_hash, ack_json FROM commands WHERE command_id=?",
            (envelope.command_id,),
        ).fetchone()
        if row is None:
            return None
        stored_hash, ack_json = row
        if stored_hash != payload_hash(envelope):
            return CommandAck(
                command_id=envelope.command_id, accepted=False,
                code=AckCode.COMMAND_ID_CONFLICT,
                reason="command_id was already used with a different payload",
            )
        first_ack = CommandAck.model_validate_json(ack_json)
        return first_ack.model_copy(update={"code": AckCode.REPLAYED})

    def record(self, envelope: CommandEnvelope, ack: CommandAck) -> None:
        """Persist the first ack of an accepted command (idempotent insert —
        a race keeps the earliest record)."""
        self._db.execute(
            """INSERT OR IGNORE INTO commands
               (command_id, payload_hash, command, issued_by, t_wall, ack_json)
               VALUES (?,?,?,?,?,?)""",
            (envelope.command_id, payload_hash(envelope), envelope.command,
             envelope.issued_by, envelope.t_wall.isoformat(),
             ack.model_dump_json()),
        )
        self._db.commit()
