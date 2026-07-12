"""JSON Schema bundle export (plan §6.7, PR C-5).

``python -m phoebe.contracts.export`` writes the deterministic schema bundle
every out-of-process consumer builds against: events, commands/acks, run
journal records, device stats.  The committed bundle is a CI gate — schema
drift fails the build until the bundle (and downstream codegen) is
regenerated, so the wire contract can never change silently.

Usage::

    python -m phoebe.contracts.export                 # rewrite the bundle
    python -m phoebe.contracts.export --check         # exit 2 on drift
    python -m phoebe.contracts.export --out other.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import models_json_schema

from .api import ApiEnvelope, ApiError, ServerMeta
from .commands import AdmissionDecision, CommandAck, CommandEnvelope
from .errors import ErrorInfo
from .events import EventBusStats, GatewayEvent, PreviewPayload
from .instruments import (
    ControllerStats,
    DeviceHealth,
    DeviceIdentity,
    DeviceStatusView,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from .run import (
    DataPointer,
    RecoveryReport,
    RunJournalRecord,
    RunManifest,
    RunResult,
)

#: Version of the bundle layout itself (not of the contracts inside it).
BUNDLE_FORMAT = 1
#: Contract schema generation (v2 = typed codes + journal + preview union).
CONTRACTS_VERSION = 2

DEFAULT_BUNDLE_PATH = Path("schemas") / "phoebe-contracts.schema.json"

_REF_TEMPLATE = "#/$defs/{model}"

#: Every named model exported to consumers.  The GatewayEvent/PreviewPayload
#: unions are added separately (they are annotated unions, not models).
_MODELS: tuple[type[BaseModel], ...] = (
    CommandEnvelope,
    CommandAck,
    AdmissionDecision,
    ErrorInfo,
    RunJournalRecord,
    RunManifest,
    RunResult,
    RecoveryReport,
    DataPointer,
    EventBusStats,
    ControllerStats,
    DeviceHealth,
    DeviceIdentity,
    InstrumentDescriptor,
    InstrumentSnapshot,
    DeviceStatusView,
    ApiEnvelope,
    ApiError,
    ServerMeta,
)

_UNIONS: dict[str, Any] = {
    "GatewayEvent": GatewayEvent,
    "PreviewPayload": PreviewPayload,
}


def build_bundle() -> dict[str, Any]:
    """One deterministic document: shared ``$defs`` + named roots."""
    keyed, defs_doc = models_json_schema(
        [(model, "serialization") for model in _MODELS],
        ref_template=_REF_TEMPLATE,
    )
    defs: dict[str, Any] = dict(defs_doc.get("$defs", {}))
    roots: dict[str, Any] = {
        model.__name__: keyed[(model, "serialization")] for model in _MODELS
    }
    for name, union in _UNIONS.items():
        schema = TypeAdapter(union).json_schema(
            ref_template=_REF_TEMPLATE, mode="serialization")
        defs.update(schema.pop("$defs", {}))
        roots[name] = schema
    return {
        "bundle_format": BUNDLE_FORMAT,
        "contracts_version": CONTRACTS_VERSION,
        "$defs": defs,
        "roots": roots,
    }


def render_bundle() -> str:
    """Canonical serialization — byte-stable across runs and platforms."""
    return json.dumps(build_bundle(), indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def check_bundle(path: Path) -> bool:
    """True when the committed bundle matches the current contracts."""
    try:
        return path.read_text(encoding="utf-8") == render_bundle()
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export (or verify) the PHOEBE contracts JSON Schema bundle")
    parser.add_argument("--out", type=Path, default=DEFAULT_BUNDLE_PATH)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed bundle instead of writing")
    args = parser.parse_args(argv)

    if args.check:
        if check_bundle(args.out):
            print(f"schema bundle {args.out} is up to date")
            return 0
        print(f"SCHEMA DRIFT: {args.out} does not match the contracts — "
              f"run `python -m phoebe.contracts.export` and commit the result",
              file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_bundle(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
