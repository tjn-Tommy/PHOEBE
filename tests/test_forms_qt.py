"""Qt renderer for schema forms (PR D-2) — offscreen; skipped without PyQt5.

The drift acceptance at the widget level: an untouched SchemaForm submits
exactly the contract defaults, and those defaults pass the strict boundary."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("PyQt5")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from phoebe.contracts.base import validate_boundary
from phoebe.core.plugin import plugin_registry
from phoebe.plugins import load_builtin_plugins
from phoebe.ui.forms import SchemaForm

load_builtin_plugins()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_untouched_forms_submit_contract_defaults(qapp):
    """H12 drift test at the widget level, for every builtin command."""
    for command in plugin_registry.commands():
        spec = plugin_registry.spec_for_command(command)
        form = SchemaForm(command, spec.config_type.model_json_schema())
        payload = form.payload()
        want = json.loads(spec.config_type().model_dump_json())
        assert payload == want, f"{command}: widget defaults drifted"
        assert validate_boundary(spec.config_type, payload) == spec.config_type()


def test_edited_form_produces_typed_payload(qapp):
    from phoebe.plugins.tpa_multiplier import TPAConfig

    form = SchemaForm("start_tpa_run", TPAConfig.model_json_schema())
    field, widget = form._widgets["max_steps"]
    widget.setValue(7)
    _, seed_widget = form._widgets["seed"]
    seed_widget.setValue(42)
    payload = form.payload()
    assert payload["max_steps"] == 7 and payload["seed"] == 42
    config = validate_boundary(TPAConfig, payload)
    assert config.max_steps == 7
