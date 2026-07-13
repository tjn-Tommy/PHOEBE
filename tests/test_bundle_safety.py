"""Profile-bundle safety matrix (plan D-4; lessons §6.2-6.3).

Everything here runs against files and cached registry facts only — the
acceptance is explicitly **zero device I/O end-to-end**: no runtime is ever
built, no controller exists, and a hostile archive can at worst produce a
typed issue list."""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import timedelta

import pytest

from phoebe.contracts.base import utc_now, validate_boundary
from phoebe.contracts.commands import AckCode
from phoebe.contracts.profile import (
    BundleFileEntry,
    BundleManifest,
    CalibrationAsset,
    EnvironmentRequirement,
    ExperimentProfile,
    RunDraft,
)
from phoebe.core.bundle import (
    create_bundle,
    import_bundle,
    preflight_bundle,
    resolve_run_draft,
)
from phoebe.core.config import parse_app_config
from phoebe.core.plugin import PluginRegistry
from phoebe.plugins.tpa_multiplier import TPAMultiplierPlugin

SLM_H, SLM_W = 60, 80


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_class(TPAMultiplierPlugin,
                            plugin_id="org.lab.tpa_multiplier")
    return registry


def _app_config():
    return parse_app_config({
        "mode": "dev",
        "instruments": [
            {"instrument_id": "slm.primary", "kind": "pattern_modulator",
             "vendor": "santec", "model": "slm-200", "role": "primary_slm",
             "backend": "sim",
             "connection": {"transport": "vendor_dll", "dll_path": "unused"}},
            {"instrument_id": "osa.main", "kind": "spectrum_analyzer",
             "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
             "backend": "sim",
             "connection": {"transport": "tcp", "host": "sim", "port": 10001}},
        ],
    })


def _profile(**overrides) -> ExperimentProfile:
    base = dict(
        profile_id="tpa-demo", name="TPA demo", plugin_id="org.lab.tpa_multiplier",
        command="start_tpa_run",
        config={"max_steps": 2, "seed": 1,
                "scan": {"center_nm": 778.0, "span_nm": 8.0, "points": 101}},
        requirements=(
            EnvironmentRequirement(role="primary_slm", kind="pattern_modulator",
                                   vendor="santec", model="slm-200"),
            EnvironmentRequirement(role="main_osa", kind="spectrum_analyzer",
                                   vendor="yokogawa", model="aq6370"),
        ),
    )
    base.update(overrides)
    return ExperimentProfile(**base)


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def _lut_asset(payload: bytes, **overrides) -> CalibrationAsset:
    base = dict(asset_id="lut-1", kind="slm_phase_lut", vendor="santec",
                model="slm-200", binding="model",
                payload_path="assets/calibration/lut.bin",
                sha256=hashlib.sha256(payload).hexdigest(),
                source_run="run-2026-07-01", generator="calibrate.py v3")
    base.update(overrides)
    return CalibrationAsset(**base)


# ------------------------------------------------------------- happy path
def test_valid_bundle_preflights_imports_and_drafts(tmp_path):
    """Lessons §6.3 acceptance: a clean sim environment turns a legal bundle
    into a RunDraft with a deterministic admission preview."""
    lut = b"\x00\x01" * 512
    zip_path = tmp_path / "profile.zip"
    manifest = create_bundle(
        zip_path, name="tpa-demo-bundle", profile=_profile(),
        assets={"assets/calibration/lut.bin": lut},
        asset_records=(_lut_asset(lut),),
        source_run="run-2026-07-01", notes="inert text only")
    assert manifest.files                    # manifest is the checksum source

    report = preflight_bundle(zip_path, registry=_registry())
    assert report.ok, report.issues
    assert report.files_verified == 3        # profile + lut + notes
    assert report.profile is not None and report.manifest is not None

    report, published = import_bundle(zip_path, tmp_path / "bundles",
                                      registry=_registry())
    assert report.ok and published is not None
    assert (published / "manifest.json").is_file()
    assert (published / "profile.json").is_file()
    assert (published / "assets/calibration/lut.bin").read_bytes() == lut

    # the published profile resolves into an unexecuted, admitted draft
    profile = validate_boundary(
        ExperimentProfile,
        json.loads((published / "profile.json").read_text("utf-8")))
    draft = resolve_run_draft(profile, registry=_registry(),
                              app_config=_app_config())
    assert draft.admission_preview is AckCode.ACCEPTED
    assert draft.bindings == {"primary_slm": "slm.primary",
                              "main_osa": "osa.main"}
    assert draft.issues == ()
    assert validate_boundary(RunDraft, json.loads(draft.model_dump_json())) == draft

    # bundles are immutable: importing again is refused, target untouched
    report2, published2 = import_bundle(zip_path, tmp_path / "bundles",
                                        registry=_registry())
    assert not report2.ok and published2 is None
    assert "bundle_exists" in _codes(report2)


# ------------------------------------------------------- hostile archives
def test_zip_slip_and_absolute_paths_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", b"x")
        zf.writestr("/rooted.txt", b"x")
        zf.writestr("c:/windows/evil.txt", b"x")
    report = preflight_bundle(evil)
    assert not report.ok
    assert {"zip_slip", "absolute_path"} <= _codes(report)


def test_symlink_entry_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        info = zipfile.ZipInfo("assets/link")
        info.external_attr = (0o120777 << 16)      # S_IFLNK
        zf.writestr(info, b"../../secret")
    report = preflight_bundle(evil)
    assert not report.ok and "symlink" in _codes(report)


def test_zip_bomb_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("assets/zeros.bin", b"\x00" * (1024 * 1024))
    report = preflight_bundle(evil)
    assert not report.ok and "zip_bomb" in _codes(report)


def test_executables_and_python_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("assets/payload.py", b"import os")
        zf.writestr("assets/tool.exe", b"MZ")
    report = preflight_bundle(evil)
    assert not report.ok
    assert _codes(report) >= {"forbidden_type"}


def test_duplicate_and_case_colliding_paths_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("assets/data.bin", b"a")
        zf.writestr("assets/DATA.bin", b"b")       # Windows case collision
    report = preflight_bundle(evil)
    assert not report.ok and "case_collision" in _codes(report)


def test_tampered_content_and_missing_files_detected(tmp_path):
    """The manifest is the single checksum source — both directions checked."""
    lut = b"\x01" * 64
    entries = (
        BundleFileEntry(path="profile.json", size_bytes=1, sha256="0" * 64),
        BundleFileEntry(path="assets/lut.bin", size_bytes=len(lut),
                        sha256=hashlib.sha256(lut).hexdigest()),
        BundleFileEntry(path="assets/gone.bin", size_bytes=4, sha256="f" * 64),
    )
    manifest = BundleManifest(name="tampered", files=entries)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", manifest.model_dump_json())
        zf.writestr("profile.json", b"{}")             # wrong size vs manifest
        zf.writestr("assets/lut.bin", b"\x02" * 64)    # wrong content
        zf.writestr("assets/extra.bin", b"unlisted")   # not in manifest
        # assets/gone.bin listed but absent
    report = preflight_bundle(evil)
    assert not report.ok
    assert {"size_mismatch", "checksum_mismatch", "unlisted_file",
            "missing_file"} <= _codes(report)


def test_unknown_bundle_format_fails_closed(tmp_path):
    manifest = BundleManifest(name="future").model_copy(
        update={"bundle_format": 99})
    evil = tmp_path / "future.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", manifest.model_dump_json())
    report = preflight_bundle(evil)
    assert not report.ok and "unknown_format" in _codes(report)


def test_missing_manifest_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("profile.json", b"{}")
    report = preflight_bundle(evil)
    assert not report.ok and "missing_manifest" in _codes(report)


def test_not_an_archive_rejected(tmp_path):
    bogus = tmp_path / "bogus.zip"
    bogus.write_bytes(b"this is not a zip")
    report = preflight_bundle(bogus)
    assert not report.ok and "bad_archive" in _codes(report)


# ---------------------------------------------------------------- hygiene
def test_secrets_and_absolute_paths_in_config_flagged(tmp_path):
    profile = _profile(config={"max_steps": 2, "seed": 1,
                               "scan": {"center_nm": 778.0, "span_nm": 8.0,
                                        "points": 101},
                               "api_key": "sk-oops",
                               "trace_name": "C:\\Users\\bob\\data"})
    zip_path = tmp_path / "leaky.zip"
    create_bundle(zip_path, name="leaky", profile=profile)
    report = preflight_bundle(zip_path, registry=_registry())
    assert not report.ok
    assert "secret_material" in _codes(report)
    assert "absolute_path_in_config" in _codes(report)


def test_expired_calibration_is_a_warning_not_a_block(tmp_path):
    lut = b"\x07" * 32
    expired = _lut_asset(lut, expires_at=utc_now() - timedelta(days=3))
    zip_path = tmp_path / "expired.zip"
    create_bundle(zip_path, name="expired", profile=_profile(),
                  assets={"assets/calibration/lut.bin": lut},
                  asset_records=(expired,))
    report = preflight_bundle(zip_path, registry=_registry())
    assert report.ok                     # explainable, operator decides
    assert "calibration_expired" in _codes(report)


def test_strict_serial_asset_without_serial_rejected(tmp_path):
    lut = b"\x07" * 32
    asset = _lut_asset(lut, binding="strict_serial", serial="")
    zip_path = tmp_path / "serial.zip"
    create_bundle(zip_path, name="serial", profile=_profile(),
                  assets={"assets/calibration/lut.bin": lut},
                  asset_records=(asset,))
    report = preflight_bundle(zip_path)
    assert not report.ok and "missing_serial" in _codes(report)


# -------------------------------------------------------------- run drafts
@pytest.mark.parametrize("mutation, expected", [
    (dict(command="warp_drive"), AckCode.UNKNOWN_COMMAND),
    (dict(config={"max_steps": "NaN"}), AckCode.INVALID_PAYLOAD),
    (dict(plugin_api=">=99"), AckCode.PLUGIN_API_INCOMPATIBLE),
])
def test_run_draft_admission_previews(mutation, expected):
    draft = resolve_run_draft(_profile(**mutation), registry=_registry(),
                              app_config=_app_config())
    assert draft.admission_preview is expected
    assert draft.detail                          # explainable, always


def test_run_draft_for_disabled_plugin():
    registry = _registry()
    registry.disable("org.lab.tpa_multiplier")
    draft = resolve_run_draft(_profile(), registry=registry)
    assert draft.admission_preview is AckCode.PLUGIN_DISABLED


def test_environment_mismatch_yields_rebind_plan_not_error():
    """Lessons §6.3: mismatch → explainable rejection/rebinding plan."""
    profile = _profile(requirements=(
        EnvironmentRequirement(role="primary_slm", kind="pattern_modulator",
                               vendor="acme", model="never-built"),
        EnvironmentRequirement(role="main_osa", kind="spectrum_analyzer",
                               binding="portable"),
    ))
    draft = resolve_run_draft(profile, registry=_registry(),
                              app_config=_app_config())
    assert draft.admission_preview is AckCode.ACCEPTED   # payload is valid
    assert draft.bindings["primary_slm"] == ""           # unbound, explained
    assert draft.bindings["main_osa"] == "osa.main"      # portable binds by kind
    assert any("requirement unmet" in issue for issue in draft.issues)
