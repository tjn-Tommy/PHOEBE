"""Type-contract layer behaviour (refactor.md §3)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from phoebe.core.config import parse_app_config
from phoebe.core.contracts import timestamps
from phoebe.core.errors import PhoebeConfigError
from phoebe.core.events import ProgressEvent, TracePreview
from phoebe.domain.spectrum import SpectrumScanConfig


def test_strict_no_string_coercion():
    with pytest.raises(ValidationError):
        SpectrumScanConfig(center_nm="778.0", span_nm=8.0, points=1001)


def test_int_to_float_still_allowed():
    cfg = SpectrumScanConfig(center_nm=778, span_nm=8, points=1001)
    assert cfg.center_nm == 778.0


def test_physical_bounds_enforced():
    with pytest.raises(ValidationError):
        SpectrumScanConfig(center_nm=-5.0, span_nm=8.0, points=1001)


def test_resolution_must_not_exceed_span():
    with pytest.raises(ValidationError):
        SpectrumScanConfig(center_nm=778.0, span_nm=1.0, points=1001,
                           resolution_nm=2.0)


def test_unknown_fields_forbidden():
    with pytest.raises(ValidationError):
        SpectrumScanConfig(center_nm=778.0, span_nm=8.0, points=1001,
                           bogus_field=1)


def test_events_are_frozen():
    ev = ProgressEvent(step=1, **timestamps())
    with pytest.raises(ValidationError):
        ev.step = 2


def test_preview_capped_at_256_points():
    with pytest.raises(ValidationError):
        TracePreview(x_nm=[0.0] * 300, y_dbm=[0.0] * 300)


def test_config_parse_and_bindings():
    raw = {
        "instruments": [
            {
                "instrument_id": "osa.main", "kind": "spectrum_analyzer",
                "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
                "backend": "sim",
                "connection": {"transport": "tcp", "host": "1.2.3.4", "port": 10001},
            }
        ],
        "plugins": {"org.lab.x": {"bindings": {"osa": "main_osa"}}},
    }
    cfg = parse_app_config(raw)
    assert cfg.plugin_bindings["org.lab.x"]["osa"] == "main_osa"
    assert cfg.role_map()["main_osa"] == "osa.main"


def test_config_duplicate_role_rejected():
    inst = {
        "instrument_id": "a", "kind": "k", "vendor": "v", "model": "m",
        "role": "dup", "backend": "sim",
        "connection": {"transport": "tcp", "host": "h", "port": 1},
    }
    other = dict(inst, instrument_id="b")
    with pytest.raises(PhoebeConfigError):
        parse_app_config({"instruments": [inst, other]})
