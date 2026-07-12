"""Generate TypeScript type declarations from the PHOEBE contracts bundle
(plan §6.7 / PR C-5: "TS types generated in a sample consumer").

Deliberately small: it covers exactly the JSON-Schema subset pydantic v2
emits for our ContractModels (objects, enums, literals, arrays, records,
unions, $refs).  Anything it does not recognize maps to ``unknown`` — the
generated file stays valid TypeScript either way.

Usage::

    python tools/gen_ts_types.py            # regenerate the .d.ts
    python tools/gen_ts_types.py --check    # exit 2 on drift (CI)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = REPO_ROOT / "schemas" / "phoebe-contracts.schema.json"
DEFAULT_OUT = REPO_ROOT / "examples" / "ts_consumer" / "phoebe-contracts.d.ts"

_PRIMITIVES = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "null": "null",
}


def _literal(value: Any) -> str:
    return json.dumps(value) if isinstance(value, str) else str(value)


def ts_type(schema: dict[str, Any] | bool) -> str:
    """Render one schema node as a TypeScript type expression."""
    if schema is True or schema == {}:
        return "unknown"
    if schema is False:
        return "never"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return _literal(schema["const"])
    if "enum" in schema:
        return " | ".join(_literal(v) for v in schema["enum"])
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema:
            parts = [ts_type(sub) for sub in schema[union_key]]
            # de-duplicate while keeping order
            return " | ".join(dict.fromkeys(parts))
    schema_type = schema.get("type")
    if schema_type == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(ts_type(s) for s in schema["prefixItems"]) + "]"
        return f"{ts_type(schema.get('items', True))}[]"
    if schema_type == "object" or "properties" in schema:
        if "properties" not in schema:
            extra = schema.get("additionalProperties", True)
            return f"Record<string, {ts_type(extra)}>"
        return _inline_object(schema)
    if isinstance(schema_type, list):
        return " | ".join(_PRIMITIVES.get(t, "unknown") for t in schema_type)
    if schema_type in _PRIMITIVES:
        return _PRIMITIVES[schema_type]
    return "unknown"


def _inline_object(schema: dict[str, Any]) -> str:
    required = set(schema.get("required", ()))
    fields = []
    for name, sub in schema.get("properties", {}).items():
        optional = "" if name in required else "?"
        fields.append(f"{name}{optional}: {ts_type(sub)}")
    return "{ " + "; ".join(fields) + " }"


def _emit_def(name: str, schema: dict[str, Any]) -> str:
    description = schema.get("description", "").strip()
    doc = f"/** {description.splitlines()[0]} */\n" if description else ""
    if schema.get("type") == "object" or "properties" in schema:
        required = set(schema.get("required", ()))
        lines = [f"{doc}export interface {name} {{"]
        for prop, sub in schema.get("properties", {}).items():
            optional = "" if prop in required else "?"
            lines.append(f"  {prop}{optional}: {ts_type(sub)};")
        lines.append("}")
        return "\n".join(lines)
    return f"{doc}export type {name} = {ts_type(schema)};"


def render(bundle: dict[str, Any]) -> str:
    out = [
        "// Generated from schemas/phoebe-contracts.schema.json — DO NOT EDIT.",
        "// Regenerate with: python tools/gen_ts_types.py",
        f"// bundle_format {bundle['bundle_format']}, "
        f"contracts_version {bundle['contracts_version']}",
        "",
    ]
    for name in sorted(bundle.get("$defs", {})):
        out.append(_emit_def(name, bundle["$defs"][name]))
        out.append("")
    for name in sorted(bundle.get("roots", {})):
        root = bundle["roots"][name]
        if "$ref" in root and root["$ref"].rsplit("/", 1)[-1] == name:
            continue                       # already emitted as a $def
        out.append(f"export type {name} = {ts_type(root)};")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    rendered = render(bundle)

    if args.check:
        try:
            current = args.out.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != rendered:
            print(f"TS TYPES DRIFT: {args.out} does not match the schema "
                  f"bundle — run `python tools/gen_ts_types.py` and commit",
                  file=sys.stderr)
            return 2
        print(f"{args.out} is up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
