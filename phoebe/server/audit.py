"""Append-only audit trail for mutating API calls (ladder rung 2 groundwork).

JSONL at ``runs/.phoebe/audit.jsonl`` — same append+flush posture as the run
journal: the audit log **never raises into request handling**; on OSError it
goes inert loudly and the API stays up (an unauditable localhost bench beats
a dead one; a *network* deployment should treat the error log as a page).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import IO

from loguru import logger

from ..contracts.base import utc_now

AUDIT_FILENAME = "audit.jsonl"


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None
        self._broken = False

    def record(self, *, actor: str, action: str, target: str = "",
               outcome: str = "") -> None:
        if self._broken:
            return
        entry = {"t_wall": utc_now().isoformat(), "actor": actor,
                 "action": action, "target": target, "outcome": outcome}
        try:
            if self._file is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self._path.open("a", encoding="utf-8")
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._file.flush()
        except OSError:
            self._broken = True
            logger.opt(exception=True).error(
                "audit log at {} is broken — auditing disabled for this process",
                self._path)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
