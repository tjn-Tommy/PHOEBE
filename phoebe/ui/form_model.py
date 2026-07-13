"""Schema-driven form metadata (plan §6.6 / PR D-2 — the H12 drift fix).

A ``FormGroup`` is derived from a plugin config's JSON Schema — the **only**
source of defaults, ranges, enums and descriptions.  Frontends render it
(Qt: ``ui/forms.py``; the web client derives the same structure in JS);
no panel may hand-copy a default again — the drift test compares
``defaults_payload`` against the pydantic model's own defaults.

Pure Python on purpose: importable (and CI-testable) without PyQt5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["int", "float", "bool", "str", "enum", "json"]


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    kind: FieldKind
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_min: bool = False
    exclusive_max: bool = False
    choices: tuple[Any, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class FormGroup:
    name: str
    items: tuple[FormField | FormGroup, ...] = ()
    description: str = ""


def _deref(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        resolved = defs.get(node["$ref"].split("/")[-1], {})
        merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        return merged
    return node


def _scalar_field(name: str, prop: dict[str, Any], default: Any) -> FormField:
    description = prop.get("description", "")
    if "enum" in prop:
        return FormField(name=name, kind="enum", default=default,
                         choices=tuple(prop["enum"]), description=description)
    kind_of_type = {"integer": "int", "number": "float", "boolean": "bool",
                    "string": "str"}
    kind = kind_of_type.get(prop.get("type", ""))
    if kind is None:
        return FormField(name=name, kind="json", default=default,
                         description=description)
    minimum = maximum = None
    exclusive_min = exclusive_max = False
    if "minimum" in prop:
        minimum = prop["minimum"]
    if "exclusiveMinimum" in prop:
        minimum, exclusive_min = prop["exclusiveMinimum"], True
    if "maximum" in prop:
        maximum = prop["maximum"]
    if "exclusiveMaximum" in prop:
        maximum, exclusive_max = prop["exclusiveMaximum"], True
    return FormField(name=name, kind=kind, default=default,  # type: ignore[arg-type]
                     minimum=minimum, maximum=maximum,
                     exclusive_min=exclusive_min, exclusive_max=exclusive_max,
                     description=description)


def _build_item(name: str, raw: dict[str, Any],
                defs: dict[str, Any]) -> FormField | FormGroup:
    prop = _deref(raw, defs)
    default = prop.get("default")
    if "anyOf" in prop:                  # Optional[X] → the non-null member
        inner = [_deref(p, defs) for p in prop["anyOf"]]
        inner = [p for p in inner if p.get("type") != "null"]
        if len(inner) == 1:
            prop = {**inner[0], "description": prop.get("description", "")}
            if default is not None or "default" not in prop:
                prop["default"] = default
            default = prop.get("default")
    if prop.get("type") == "object" and "properties" in prop:
        overrides = default if isinstance(default, dict) else {}
        return _build_group(name, prop, defs, overrides)
    return _scalar_field(name, prop, default)


def _apply_override(item: FormField | FormGroup,
                    value: Any) -> FormField | FormGroup:
    if isinstance(item, FormField):
        return FormField(name=item.name, kind=item.kind, default=value,
                         minimum=item.minimum, maximum=item.maximum,
                         exclusive_min=item.exclusive_min,
                         exclusive_max=item.exclusive_max,
                         choices=item.choices, description=item.description)
    if isinstance(value, dict):
        return FormGroup(
            name=item.name,
            items=tuple(_apply_override(i, value[i.name]) if i.name in value
                        else i for i in item.items),
            description=item.description)
    return item


def _build_group(name: str, node: dict[str, Any], defs: dict[str, Any],
                 overrides: dict[str, Any] | None = None) -> FormGroup:
    items: list[FormField | FormGroup] = []
    for prop_name, raw in (node.get("properties") or {}).items():
        item = _build_item(prop_name, raw, defs)
        if overrides and prop_name in overrides:
            item = _apply_override(item, overrides[prop_name])
        items.append(item)
    return FormGroup(name=name, items=tuple(items),
                     description=node.get("description", ""))


def build_form_model(schema: dict[str, Any]) -> FormGroup:
    """JSON Schema (``model_json_schema()``) → renderable form tree."""
    return _build_group("", schema, schema.get("$defs", {}))


def defaults_payload(group: FormGroup) -> dict[str, Any]:
    """The payload an untouched form submits — must equal the contract's own
    defaults (the H12 drift test asserts exactly this)."""
    payload: dict[str, Any] = {}
    for item in group.items:
        if isinstance(item, FormGroup):
            payload[item.name] = defaults_payload(item)
        else:
            payload[item.name] = item.default
    return payload
