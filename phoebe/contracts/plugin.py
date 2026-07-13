"""Plugin platform contracts (plan §6.6, PR D-1).

``PluginManifest`` holds **static facts only** — id, version, API range,
commands.  It never becomes mutable runtime state (AstrBot's
mutable-metadata trap): device requirements stay in the entrypoint's
``Depends`` annotations, and the commands listed here are
consistency-checked against the registered entrypoints at load time.

``PluginStatusView`` is the availability report (A8): one row per plugin —
loaded, disabled, or failed-with-error — so one broken plugin degrades the
platform visibly instead of aborting startup.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from .base import ContractModel
from .errors import ErrorInfo


class PluginManifest(ContractModel):
    """Static facts about one plugin (``plugin.toml`` for directory plugins;
    derived from the class for builtins)."""

    plugin_id: str = Field(min_length=1)
    name: str = ""
    version: str = "0.0.0"               # semver / PEP 440 version string
    #: PEP 440 range over the kernel's PLUGIN_API_VERSION (A9) — checked at
    #: load with a clear message, never mid-run.
    api: str = ">=1,<2"
    entry: str = ""                      # entry module file, directory plugins only
    config_schema_version: int = 1
    commands: tuple[str, ...] = ()       # consistency-checked at registration
    artifact_types: tuple[str, ...] = ()
    requires_hardware: bool = False
    ui_hints: dict[str, str] = Field(default_factory=dict)
    description: str = ""


def manifest_hash(manifest: PluginManifest) -> str:
    """Canonical content hash (recorded in run manifests / bundles)."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


PluginState = Literal["loaded", "disabled", "failed"]


class PluginStatusView(ContractModel):
    """One row of the plugin availability report (A8)."""

    plugin_id: str
    state: PluginState
    version: str = ""
    api: str = ""
    commands: tuple[str, ...] = ()
    source: str = "builtin"              # "builtin" or the plugin directory
    error: ErrorInfo | None = None       # set when state == "failed"
    detail: str | None = None            # traceback tail / requirements note
