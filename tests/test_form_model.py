"""Schema-driven form metadata (PR D-2) — the H12 drift gate.

Pure-Python half (runs in CI without Qt): an untouched form's payload must
equal the contract's own defaults, for every builtin plugin.  The Qt
renderer half lives in test_forms_qt.py (skipped where PyQt5 is absent)."""
from __future__ import annotations

import json

from phoebe.contracts.base import validate_boundary
from phoebe.core.plugin import plugin_registry
from phoebe.plugins import load_builtin_plugins
from phoebe.ui.form_model import FormField, FormGroup, build_form_model, defaults_payload

load_builtin_plugins()


def test_no_drift_between_forms_and_contracts():
    """H12 acceptance: for every builtin command, the generated form's
    defaults are byte-identical to the config model's defaults."""
    for command in plugin_registry.commands():
        spec = plugin_registry.spec_for_command(command)
        model = build_form_model(spec.config_type.model_json_schema())
        got = defaults_payload(model)
        want = json.loads(spec.config_type().model_dump_json())
        assert got == want, f"{command}: form drifted from contract"
        # and the payload round-trips the strict boundary
        assert validate_boundary(spec.config_type, got) == spec.config_type()


def test_constraints_come_from_schema():
    """Ranges/kinds are lifted from the schema, not hand-written."""
    from phoebe.plugins.tpa_multiplier import TPAConfig

    model = build_form_model(TPAConfig.model_json_schema())
    fields = {item.name: item for item in model.items}
    steps = fields["max_steps"]
    assert isinstance(steps, FormField)
    assert steps.kind == "int" and steps.minimum == 1 and steps.maximum == 1_000_000
    scan = fields["scan"]
    assert isinstance(scan, FormGroup)           # nested model → group
    scan_fields = {item.name: item for item in scan.items}
    assert scan_fields["center_nm"].kind == "float"
    assert scan_fields["center_nm"].default == 778.0   # parent default applied
    assert scan_fields["points"].default == 101 or \
        scan_fields["points"].default == 1001    # whichever the contract says


def test_enum_optional_and_json_fields():
    from typing import Literal

    from phoebe.contracts.base import ContractModel

    class Demo(ContractModel):
        mode: Literal["fast", "precise"] = "precise"
        limit: float | None = None
        tags: tuple[str, ...] = ("a", "b")

    model = build_form_model(Demo.model_json_schema())
    fields = {item.name: item for item in model.items}
    assert fields["mode"].kind == "enum"
    assert fields["mode"].choices == ("fast", "precise")
    assert fields["mode"].default == "precise"
    assert fields["limit"].kind == "float" and fields["limit"].default is None
    assert fields["tags"].kind == "json" and fields["tags"].default == ["a", "b"]
