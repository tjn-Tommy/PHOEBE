"""Profile & bundle contracts (plan D-4; lessons §6).

A portable "Profile Bundle" is **not** a zipped AppConfig: the full config
carries IPs, VISA resources and DLL absolute paths that must never travel.
A bundle carries logical requirements plus immutable assets, and the target
machine re-binds them to local devices:

* ``CalibrationAsset`` — immutable LUT / wavelength map / fit result with
  provenance and an applicability boundary (binding policy, expiry).
* ``ExperimentProfile`` — the strongly-typed, reproducible experiment
  parameters for one plugin command + its logical environment requirements.
* ``BundleManifest`` — the only checksum source: every file in the archive
  is listed with size + SHA-256.  A checksum detects tampering only if the
  manifest itself is trusted — it is **not** a signature.
* ``RunDraft`` — what a profile resolves to on *this* machine: an unexecuted
  command payload + role bindings + a deterministic admission preview.
  Drafts never auto-run.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .base import AwareDatetime, ContractModel
from .commands import AckCode

#: Bundle layout version — unknown versions fail closed at preflight.
BUNDLE_FORMAT = 1

#: How an asset/requirement may migrate between machines (lessons §6.1):
#: ``strict_serial`` = only the exact physical unit; ``model`` = any unit of
#: the same vendor+model; ``portable`` = any instrument of the kind.
BindingPolicy = Literal["strict_serial", "model", "portable"]


class BundleFileEntry(ContractModel):
    """One file in the archive, as declared by the manifest."""

    path: str                            # canonical relative POSIX path
    media_type: str = "application/octet-stream"
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    schema_version: int = 1
    asset_type: str = ""                 # "calibration" / "profile" / "note" ...


class CalibrationAsset(ContractModel):
    """Immutable calibration artifact with provenance + applicability."""

    asset_id: str = Field(min_length=1)
    kind: str                            # e.g. "slm_phase_lut"
    vendor: str = ""
    model: str = ""
    serial: str = ""                     # required by binding="strict_serial"
    binding: BindingPolicy = "model"
    payload_path: str                    # relative path inside the bundle
    sha256: str = Field(min_length=64, max_length=64)
    created_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    source_run: str = ""                 # RunId back-link (lessons §6.3)
    generator: str = ""                  # algorithm/tool + version
    metadata: dict[str, str] = Field(default_factory=dict)


class EnvironmentRequirement(ContractModel):
    """A logical device the profile needs — re-bound on the target machine."""

    role: str                            # DI role the plugin expects
    kind: str                            # capability kind
    vendor: str = ""                     # required when binding != "portable"
    model: str = ""
    binding: BindingPolicy = "model"


class ExperimentProfile(ContractModel):
    """Reproducible experiment parameters for one plugin command."""

    profile_id: str = Field(min_length=1)
    name: str = ""
    plugin_id: str
    plugin_api: str = ">=1,<2"           # PEP 440 range, like the manifest
    command: str
    config: dict[str, Any] = Field(default_factory=dict)
    requirements: tuple[EnvironmentRequirement, ...] = ()
    calibration: tuple[str, ...] = ()    # CalibrationAsset ids used
    notes: str = ""


class BundleManifest(ContractModel):
    """``manifest.json`` — the archive's single source of truth."""

    bundle_format: int = BUNDLE_FORMAT
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    created_at: AwareDatetime | None = None
    contracts_version: int = 2
    files: tuple[BundleFileEntry, ...] = ()
    assets: tuple[CalibrationAsset, ...] = ()
    source_run: str = ""                 # bundle exported from this run, if any


class BundleIssue(ContractModel):
    """One preflight finding: a stable machine code + human detail."""

    severity: Literal["error", "warning"]
    code: str                            # "zip_slip", "symlink", "zip_bomb", ...
    detail: str


class BundlePreflight(ContractModel):
    """Read-only import plan (lessons §6.2): what would be admitted, reused,
    or rejected — produced without extracting, importing, or touching any
    device."""

    ok: bool
    issues: tuple[BundleIssue, ...] = ()
    manifest: BundleManifest | None = None
    profile: ExperimentProfile | None = None
    files_verified: int = 0


class RunDraft(ContractModel):
    """A profile resolved against this machine: unexecuted by definition."""

    profile_id: str
    plugin_id: str
    command: str
    payload: dict[str, Any] = Field(default_factory=dict)
    #: role → locally bound instrument_id ("" = unbound; see issues)
    bindings: dict[str, str] = Field(default_factory=dict)
    #: Deterministic route/validate preview — never device I/O.
    admission_preview: AckCode
    detail: str | None = None
    issues: tuple[str, ...] = ()
