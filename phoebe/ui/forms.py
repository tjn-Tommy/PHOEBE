"""Qt renderer for schema-derived forms (plan §6.6, PR D-2).

``SchemaForm`` renders a :mod:`phoebe.ui.form_model` tree — every default,
range and enum comes from the plugin's own JSON Schema, so a form can no
longer drift from its contract (H12).  Dedicated rich panels remain allowed,
but they must source their constraints from the same ``FormGroup`` metadata.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from .form_model import FormField, FormGroup, build_form_model

_INT32_MAX = 2**31 - 1


def _int_widget(field: FormField) -> QSpinBox:
    box = QSpinBox()
    low = int(field.minimum) if field.minimum is not None else -_INT32_MAX
    high = int(field.maximum) if field.maximum is not None else _INT32_MAX
    if field.exclusive_min:
        low += 1
    if field.exclusive_max:
        high -= 1
    box.setRange(max(low, -_INT32_MAX), min(high, _INT32_MAX))
    if field.default is not None:
        box.setValue(int(field.default))
    return box


def _float_widget(field: FormField) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setDecimals(6)
    epsilon = 1e-6
    low = field.minimum if field.minimum is not None else -1e12
    high = field.maximum if field.maximum is not None else 1e12
    box.setRange(low + (epsilon if field.exclusive_min else 0.0),
                 high - (epsilon if field.exclusive_max else 0.0))
    if field.default is not None:
        box.setValue(float(field.default))
    return box


class _GroupForm(QWidget):
    """Recursive renderer for one FormGroup."""

    def __init__(self, group: FormGroup) -> None:
        super().__init__()
        self._widgets: dict[str, tuple[FormField, QWidget]] = {}
        self._groups: dict[str, _GroupForm] = {}
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for item in group.items:
            if isinstance(item, FormGroup):
                sub = _GroupForm(item)
                box = QGroupBox(item.name)
                inner = QFormLayout(box)
                inner.setContentsMargins(4, 4, 4, 4)
                inner.addRow(sub)
                self._groups[item.name] = sub
                layout.addRow(box)
            else:
                widget = self._widget_for(item)
                if item.description:
                    widget.setToolTip(item.description)
                self._widgets[item.name] = (item, widget)
                layout.addRow(item.name, widget)

    def _widget_for(self, field: FormField) -> QWidget:
        if field.kind == "int":
            return _int_widget(field)
        if field.kind == "float":
            return _float_widget(field)
        if field.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(field.default))
            return box
        if field.kind == "enum":
            combo = QComboBox()
            for choice in field.choices:
                combo.addItem(str(choice), userData=choice)
            if field.default in field.choices:
                combo.setCurrentIndex(field.choices.index(field.default))
            return combo
        if field.kind == "json":
            edit = QLineEdit(json.dumps(field.default))
            edit.setPlaceholderText("JSON")
            return edit
        return QLineEdit("" if field.default is None else str(field.default))

    def payload(self) -> dict[str, Any]:
        """Collect current values; raises ValueError on bad JSON text."""
        out: dict[str, Any] = {}
        for name, (field, widget) in self._widgets.items():
            if field.kind in ("int", "float"):
                out[name] = widget.value()
            elif field.kind == "bool":
                out[name] = widget.isChecked()
            elif field.kind == "enum":
                out[name] = widget.currentData()
            elif field.kind == "json":
                try:
                    out[name] = json.loads(widget.text())
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{name}: invalid JSON — {exc}") from exc
            else:
                out[name] = widget.text()
        for name, sub in self._groups.items():
            out[name] = sub.payload()
        return out


class SchemaForm(_GroupForm):
    """One command's parameter form, generated from its config schema."""

    def __init__(self, command: str, schema: dict[str, Any]) -> None:
        super().__init__(build_form_model(schema))
        self.command = command
