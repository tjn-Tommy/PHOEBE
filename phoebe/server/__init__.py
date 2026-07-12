"""HTTP adapter over the services layer (plan Phase E, §6.7).

A thin FastAPI transport: every route calls the same ``ServiceHub`` the PyQt
shell uses in-process, so the boundary is identical — commands in through the
Gateway, typed acks/events out, zero controller reach-ins (enforced by
import-linter, exactly like the UI).

FastAPI/uvicorn are optional dependencies (``pip install -e .[server]``)
imported only inside this package — core, sim mode, tests and the PyQt UI
never need them.

Run it::

    python -m phoebe.server --config config/sim.toml
"""
from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:  # lazy: keep fastapi optional
    if name == "create_app":
        from .app import create_app
        return create_app
    raise AttributeError(name)
