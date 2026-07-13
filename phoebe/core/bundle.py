"""Profile-bundle export / preflight / import (plan D-4; lessons §6.2).

The import protocol is **read-only until the operator publishes**:

1. ``preflight_bundle`` inspects the archive without extracting: structural
   safety (zip-slip, symlinks, bombs, duplicate/case-colliding paths,
   forbidden file types, size/count caps), manifest-driven checksum
   verification, profile/plugin compatibility, calibration applicability.
   It returns a typed plan — never raises for a bad bundle, never touches a
   device, never imports Python from the archive.
2. ``import_bundle`` re-runs preflight, extracts into a same-volume temp
   directory, then publishes with one atomic rename.  Existing bundles are
   never overwritten.
3. ``resolve_run_draft`` turns a profile into an unexecuted ``RunDraft``
   with a deterministic admission preview (route + payload validation only)
   and a role→local-instrument rebinding plan.  Drafts never auto-run.

``create_bundle`` is the matching exporter (checksums computed here so the
manifest is the single source of truth).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..contracts.base import utc_now, validate_boundary
from ..contracts.commands import AckCode
from ..contracts.profile import (
    BUNDLE_FORMAT,
    BundleFileEntry,
    BundleIssue,
    BundleManifest,
    BundlePreflight,
    CalibrationAsset,
    ExperimentProfile,
    RunDraft,
)
from .plugin import PLUGIN_API_VERSION, PluginLoadError, api_compatible

if TYPE_CHECKING:
    from .config import AppConfig
    from .plugin import PluginRegistry

MANIFEST_NAME = "manifest.json"
PROFILE_NAME = "profile.json"

MAX_FILES = 256
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 100          # checked for files > 64 KiB
_RATIO_FLOOR = 64 * 1024

#: Executables and importable code never ride a bundle (lessons §6.2).
FORBIDDEN_SUFFIXES = frozenset({
    ".py", ".pyc", ".pyd", ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd",
    ".ps1", ".sh", ".msi", ".scr", ".com", ".js", ".vbs", ".jar",
})

_SECRET_KEY_MARKERS = ("token", "password", "secret", "api_key", "apikey")


class _Findings:
    def __init__(self) -> None:
        self.issues: list[BundleIssue] = []

    def error(self, code: str, detail: str) -> None:
        self.issues.append(BundleIssue(severity="error", code=code, detail=detail))

    def warning(self, code: str, detail: str) -> None:
        self.issues.append(BundleIssue(severity="warning", code=code, detail=detail))

    @property
    def failed(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def report(self, *, manifest: BundleManifest | None = None,
               profile: ExperimentProfile | None = None,
               files_verified: int = 0) -> BundlePreflight:
        return BundlePreflight(ok=not self.failed, issues=tuple(self.issues),
                               manifest=manifest, profile=profile,
                               files_verified=files_verified)


def _check_entry_paths(infos: list[zipfile.ZipInfo], f: _Findings) -> set[str]:
    """Structural path safety; returns the set of regular-file names."""
    names: set[str] = set()
    seen_folded: set[str] = set()
    total = 0
    if len(infos) > MAX_FILES:
        f.error("too_many_files", f"{len(infos)} entries (max {MAX_FILES})")
        return names
    for zi in infos:
        name = zi.filename
        if "\\" in name:
            f.error("bad_path", f"{name!r} contains a backslash")
            continue
        pure = PurePosixPath(name)
        if pure.is_absolute() or (len(name) > 1 and name[1] == ":"):
            f.error("absolute_path", name)
            continue
        if ".." in pure.parts:
            f.error("zip_slip", f"{name!r} escapes the extraction root")
            continue
        mode = (zi.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            f.error("symlink", name)
            continue
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            f.error("special_file", f"{name!r} (mode {mode:o})")
            continue
        if zi.is_dir():
            continue
        if name in names:
            f.error("duplicate_path", name)
            continue
        folded = name.lower()
        if folded in seen_folded:
            f.error("case_collision", f"{name!r} collides case-insensitively")
            continue
        names.add(name)
        seen_folded.add(folded)
        if pure.suffix.lower() in FORBIDDEN_SUFFIXES:
            f.error("forbidden_type", name)
        if zi.file_size > MAX_FILE_BYTES:
            f.error("file_too_large",
                    f"{name}: {zi.file_size} bytes (max {MAX_FILE_BYTES})")
        if (zi.file_size > _RATIO_FLOOR and zi.compress_size > 0
                and zi.file_size / zi.compress_size > MAX_COMPRESSION_RATIO):
            f.error("zip_bomb",
                    f"{name}: compression ratio "
                    f"{zi.file_size / zi.compress_size:.0f}")
        total += zi.file_size
    if total > MAX_TOTAL_BYTES:
        f.error("total_too_large", f"{total} bytes uncompressed (max {MAX_TOTAL_BYTES})")
    return names


def _scan_config_hygiene(node: Any, path: str, f: _Findings) -> None:
    """Secrets and machine-local absolute paths must not travel (lessons §6.2)."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            if any(marker in key_l for marker in _SECRET_KEY_MARKERS):
                f.error("secret_material",
                        f"profile config key {path + '.' + str(key)!r} — store an "
                        "external key *name*, never the secret itself")
            _scan_config_hygiene(value, f"{path}.{key}", f)
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            _scan_config_hygiene(value, f"{path}[{i}]", f)
    elif isinstance(node, str):
        if node.startswith("/") or (len(node) > 2 and node[1] == ":" and
                                    node[2] in "\\/"):
            f.warning("absolute_path_in_config",
                      f"{path} looks like a machine-local absolute path: {node!r}")


def _check_assets(manifest: BundleManifest, declared: dict[str, BundleFileEntry],
                  f: _Findings) -> None:
    now = utc_now()
    for asset in manifest.assets:
        entry = declared.get(asset.payload_path)
        if entry is None:
            f.error("missing_asset",
                    f"asset {asset.asset_id!r} payload {asset.payload_path!r} "
                    "is not in the manifest file list")
        elif entry.sha256 != asset.sha256:
            f.error("asset_checksum",
                    f"asset {asset.asset_id!r} sha does not match its file entry")
        if asset.binding == "strict_serial" and not asset.serial:
            f.error("missing_serial",
                    f"asset {asset.asset_id!r} is strict_serial but has no serial")
        if asset.expires_at is not None and asset.expires_at < now:
            f.warning("calibration_expired",
                      f"asset {asset.asset_id!r} expired {asset.expires_at:%Y-%m-%d}")


def preflight_bundle(path: str | Path, *,
                     registry: PluginRegistry | None = None) -> BundlePreflight:
    """Read-only import plan.  Loads no DLL, imports no Python, connects to
    no instrument — files and cached registry facts only."""
    f = _Findings()
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        f.error("bad_archive", str(exc))
        return f.report()

    with zf:
        names = _check_entry_paths(zf.infolist(), f)
        if f.failed:
            return f.report()            # structurally unsafe: stop reading

        if MANIFEST_NAME not in names:
            f.error("missing_manifest", f"bundle has no {MANIFEST_NAME}")
            return f.report()
        raw = zf.read(MANIFEST_NAME)
        if len(raw) > MAX_MANIFEST_BYTES:
            f.error("bad_manifest", "manifest exceeds size cap")
            return f.report()
        try:
            manifest = validate_boundary(BundleManifest, json.loads(raw))
        except Exception as exc:
            f.error("bad_manifest", str(exc))
            return f.report()
        if manifest.bundle_format != BUNDLE_FORMAT:
            f.error("unknown_format",     # fail closed on newer formats
                    f"bundle_format {manifest.bundle_format} not supported "
                    f"(this kernel reads {BUNDLE_FORMAT})")
            return f.report(manifest=manifest)

        # manifest is the single checksum source: verify both directions
        declared = {e.path: e for e in manifest.files}
        verified = 0
        for name in sorted(names - {MANIFEST_NAME}):
            entry = declared.get(name)
            if entry is None:
                f.error("unlisted_file", name)
                continue
            data = zf.read(name)
            if len(data) != entry.size_bytes:
                f.error("size_mismatch", f"{name}: {len(data)} bytes, "
                                         f"manifest says {entry.size_bytes}")
            elif hashlib.sha256(data).hexdigest() != entry.sha256:
                f.error("checksum_mismatch", name)
            else:
                verified += 1
        for missing in sorted(set(declared) - names):
            f.error("missing_file", missing)

        profile: ExperimentProfile | None = None
        if PROFILE_NAME not in names:
            f.error("missing_profile", f"bundle has no {PROFILE_NAME}")
        else:
            try:
                profile = validate_boundary(
                    ExperimentProfile, json.loads(zf.read(PROFILE_NAME)))
            except Exception as exc:
                f.error("bad_profile", str(exc))

    if profile is not None:
        _scan_config_hygiene(profile.config, "config", f)
        try:
            if not api_compatible(profile.plugin_api):
                f.error("plugin_api",
                        f"profile needs plugin API {profile.plugin_api!r}; "
                        f"kernel implements {PLUGIN_API_VERSION}")
        except PluginLoadError as exc:
            f.error("plugin_api", str(exc))
        if registry is not None:
            spec = registry.spec_for_command(profile.command)
            if spec is None:
                f.warning("unknown_command",
                          f"command {profile.command!r} is not available on "
                          "this machine — rebind or install the plugin")
            else:
                try:
                    validate_boundary(spec.config_type, profile.config)
                except Exception as exc:
                    f.error("invalid_config", str(exc))
    _check_assets(manifest, declared, f)
    return f.report(manifest=manifest, profile=profile, files_verified=verified)


def import_bundle(path: str | Path, dest_root: str | Path, *,
                  registry: PluginRegistry | None = None
                  ) -> tuple[BundlePreflight, Path | None]:
    """Preflight, then extract to a same-volume temp dir and publish with one
    atomic rename.  Never overwrites an existing bundle."""
    report = preflight_bundle(path, registry=registry)
    if not report.ok or report.manifest is None:
        return report, None
    dest_root = Path(dest_root)
    target = dest_root / report.manifest.name
    if target.exists():
        issue = BundleIssue(severity="error", code="bundle_exists",
                            detail=f"{target} already exists — bundles are "
                                   "immutable, pick a new name")
        return report.model_copy(update={
            "ok": False, "issues": (*report.issues, issue)}), None

    dest_root.mkdir(parents=True, exist_ok=True)
    staging = dest_root / f".incoming-{uuid.uuid4().hex[:8]}"
    try:
        declared = {e.path: e for e in report.manifest.files}
        with zipfile.ZipFile(path) as zf:
            for name in (MANIFEST_NAME, *sorted(declared)):
                data = zf.read(name)
                entry = declared.get(name)
                if entry is not None and \
                        hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise ValueError(f"checksum changed during import: {name}")
                out = staging / PurePosixPath(name)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
        staging.rename(target)           # atomic publish (same volume)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        issue = BundleIssue(severity="error", code="import_failed",
                            detail=f"{type(exc).__name__}: {exc}")
        return report.model_copy(update={
            "ok": False, "issues": (*report.issues, issue)}), None
    return report, target


def resolve_run_draft(profile: ExperimentProfile, *, registry: PluginRegistry,
                      app_config: AppConfig | None = None) -> RunDraft:
    """Deterministic admission preview + rebinding plan — zero device I/O.
    An environment mismatch produces an explainable draft, never an error."""
    issues: list[str] = []
    detail: str | None = None
    spec = registry.spec_for_command(profile.command)

    if spec is None:
        preview = AckCode.UNKNOWN_COMMAND
        detail = f"command {profile.command!r} not available on this machine"
    elif registry.is_disabled(spec.plugin_id):
        preview = AckCode.PLUGIN_DISABLED
        detail = f"plugin {spec.plugin_id!r} is disabled"
    else:
        if spec.plugin_id != profile.plugin_id:
            issues.append(f"command {profile.command!r} is owned by "
                          f"{spec.plugin_id!r}, profile says {profile.plugin_id!r}")
        try:
            compatible = api_compatible(profile.plugin_api)
        except PluginLoadError as exc:
            compatible, detail = False, str(exc)
        if not compatible:
            preview = AckCode.PLUGIN_API_INCOMPATIBLE
            detail = detail or (f"profile needs plugin API "
                                f"{profile.plugin_api!r}; kernel implements "
                                f"{PLUGIN_API_VERSION}")
        else:
            try:
                validate_boundary(spec.config_type, profile.config)
                preview = AckCode.ACCEPTED
            except Exception as exc:
                preview = AckCode.INVALID_PAYLOAD
                detail = str(exc)

    bindings: dict[str, str] = {}
    if app_config is not None:
        for req in profile.requirements:
            if req.binding == "strict_serial":
                bindings[req.role] = ""
                issues.append(f"requirement {req.role!r}: strict_serial assets "
                              "cannot be verified against this config — "
                              "operator must confirm the physical unit")
                continue
            candidates = [c for c in app_config.instruments
                          if c.kind == req.kind
                          and (req.binding == "portable"
                               or (c.vendor == req.vendor and c.model == req.model))]
            exact = [c for c in candidates if c.role == req.role]
            chosen = (exact or candidates)[:1]
            if chosen:
                bindings[req.role] = str(chosen[0].instrument_id)
            else:
                bindings[req.role] = ""
                issues.append(
                    f"requirement unmet: no local instrument matches "
                    f"kind={req.kind!r} vendor={req.vendor!r} "
                    f"model={req.model!r} (binding={req.binding})")

    return RunDraft(profile_id=profile.profile_id, plugin_id=profile.plugin_id,
                    command=profile.command, payload=dict(profile.config),
                    bindings=bindings, admission_preview=preview,
                    detail=detail, issues=tuple(issues))


def create_bundle(path: str | Path, *, name: str, profile: ExperimentProfile,
                  assets: dict[str, bytes] | None = None,
                  asset_records: tuple[CalibrationAsset, ...] = (),
                  source_run: str = "", notes: str = "") -> BundleManifest:
    """Export a bundle: content is hashed here, so the manifest is the single
    source of truth (lessons §6.1).  ``assets`` maps archive-relative paths
    to payload bytes; ``asset_records`` must reference those paths."""
    files: dict[str, bytes] = {
        PROFILE_NAME: profile.model_dump_json(indent=2).encode()}
    if notes:
        files["notes.md"] = notes.encode()
    for rel, data in (assets or {}).items():
        files[rel] = data

    entries = tuple(
        BundleFileEntry(
            path=rel,
            media_type=("application/json" if rel.endswith(".json")
                        else "text/markdown" if rel.endswith(".md")
                        else "application/octet-stream"),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            asset_type=("profile" if rel == PROFILE_NAME
                        else "note" if rel == "notes.md" else "calibration"),
        )
        for rel, data in sorted(files.items()))
    manifest = BundleManifest(name=name, created_at=utc_now(),
                              files=entries, assets=asset_records,
                              source_run=source_run)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME,
                    manifest.model_dump_json(indent=2).encode())
        for rel, data in sorted(files.items()):
            zf.writestr(rel, data)
    return manifest
