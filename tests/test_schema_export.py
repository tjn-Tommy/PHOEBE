"""Schema export + codegen drift (plan §6.7, PR C-5).

The committed bundle and the generated TypeScript declarations must always
match the live contracts — running these tests IS the drift gate (CI also
runs the CLI ``--check`` variants for a readable failure)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from phoebe.contracts.export import (
    DEFAULT_BUNDLE_PATH,
    build_bundle,
    check_bundle,
    render_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from gen_ts_types import DEFAULT_OUTS as TS_OUTS  # noqa: E402
from gen_ts_types import render as render_ts      # noqa: E402


def test_bundle_is_deterministic():
    assert render_bundle() == render_bundle()


def test_bundle_contains_the_wire_surface():
    bundle = build_bundle()
    roots = bundle["roots"]
    for name in ("CommandEnvelope", "CommandAck", "AdmissionDecision",
                 "ErrorInfo", "GatewayEvent", "PreviewPayload",
                 "RunJournalRecord", "RunManifest", "RunResult",
                 "RecoveryReport", "ControllerStats", "DeviceStatusView",
                 "EventBusStats"):
        assert name in roots, f"{name} missing from the schema bundle"
    # discriminated unions keep their discriminators (frontend switch keys)
    assert bundle["roots"]["GatewayEvent"]["discriminator"]["propertyName"] == "event_type"
    assert bundle["roots"]["PreviewPayload"]["discriminator"]["propertyName"] == "preview_type"
    # ack codes are enumerated in the schema, not free text
    ack_code = bundle["$defs"]["AckCode"]
    assert "device_busy" in ack_code["enum"]
    assert "replayed" in ack_code["enum"]


def test_committed_bundle_matches_contracts():
    """Fails when a contract changed without regenerating the bundle:
    ``python -m phoebe.contracts.export``"""
    assert check_bundle(REPO_ROOT / DEFAULT_BUNDLE_PATH), (
        "schema drift — run `python -m phoebe.contracts.export` and commit"
    )


def test_committed_ts_types_match_bundle():
    """Fails when the bundle changed without regenerating every TS consumer
    (sample consumer + desktop client): ``python tools/gen_ts_types.py``"""
    bundle = json.loads((REPO_ROOT / DEFAULT_BUNDLE_PATH)
                        .read_text(encoding="utf-8"))
    rendered = render_ts(bundle)
    for out in TS_OUTS:
        assert out.read_text(encoding="utf-8") == rendered, (
            f"TS types drift in {out} — run `python tools/gen_ts_types.py` "
            "and commit"
        )


def test_ts_types_look_like_typescript():
    text = TS_OUTS[0].read_text(encoding="utf-8")
    assert "export interface CommandAck {" in text
    assert "code: AckCode;" in text
    assert 'export type AckCode = "accepted"' in text
    assert "export type GatewayEvent =" in text
