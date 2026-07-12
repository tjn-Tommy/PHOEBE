"""Single-instance file lock (plan §3.1 A7).

Two PHOEBE processes opening the same VISA resource or vendor DLL is a
hardware fault, not an inconvenience — the second instance must refuse to
start.  The lock is an OS-level byte lock (msvcrt on Windows, fcntl
elsewhere), so it dies with the process: a crashed instance never leaves a
stale lock behind.
"""
from __future__ import annotations

import os
from pathlib import Path


class AnotherInstanceRunningError(RuntimeError):
    """The lock file is held by a live PHOEBE process."""


class SingleInstanceLock:
    """``with SingleInstanceLock(path):`` or explicit ``acquire()``/``release()``."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> SingleInstanceLock:
        if self._fh is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AnotherInstanceRunningError(
                f"another PHOEBE instance is already running (lock: {self._path})"
            ) from exc
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass                       # process exit releases it regardless
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> SingleInstanceLock:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()
