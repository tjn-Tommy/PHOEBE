"""Plugin conformance suite (plan §6.6, PR D-3).

Static checks that keep the plugin contract honest — run in CI for the
builtin plugins (``tests/test_plugin_conformance.py``) and locally against
third-party registries.  Each check returns violation strings; an empty
report means the plugin conforms.

What is checked (refactor.md §18 hard rules + plan §6.6):

* manifest completeness: id, parseable version, compatible PEP 440 API
  range, commands consistent with the registered entrypoints;
* config model: a strict ``ContractModel`` whose JSON schema exports (the
  single source of truth for every form, H12);
* entrypoint shape: ``async (config, ctx, *devices)`` with every device
  parameter a ``Depends`` bound to a registered capability protocol;
* zero locks / zero manual sleeps in the plugin class;
* checkpointing: the entrypoint awaits ``ctx.checkpoint`` (or delegates to
  ``grid_scan``, which checkpoints per point) — the static face of
  "pausable/cancellable under sim", whose behavioral half is the e2e suite;
* import discipline (B5): the defining module imports only ``phoebe.api``,
  ``phoebe.domain``, ``phoebe.contracts`` and capability protocols from the
  ``phoebe.*`` tree — never core/transports/instrument implementations/UI.
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from typing import Any

from .contracts import ContractModel
from .di import Depends, kind_of_protocol
from .plugin import PluginLoadError, PluginRegistry, PluginSpec, api_compatible
from .task_manager import RunContext

#: The sanctioned phoebe import surface for plugin code (B5).
ALLOWED_IMPORT_PREFIXES = (
    "phoebe.api",
    "phoebe.contracts",
    "phoebe.domain",
    "phoebe.instruments.protocols",
)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """'from ..core import x' inside phoebe.plugins.foo → 'phoebe.core'."""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)] if level > 0 else parts
    return ".".join([*base, module] if module else base)


def _import_violations(module: Any) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):
        return [f"cannot read source of module {module.__name__!r}"]
    package = module.__name__.rsplit(".", 1)[0] if "." in module.__name__ \
        else module.__name__
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                targets.append(_resolve_relative(package, node.level, node.module))
            elif node.module:
                targets.append(node.module)
    for target in targets:
        if target == "threading" or target.startswith("threading."):
            violations.append("imports threading — plugins contain zero locks")
        if (target == "phoebe" or target.startswith("phoebe.")) and not any(
                target == p or target.startswith(p + ".")
                for p in ALLOWED_IMPORT_PREFIXES):
            violations.append(
                f"imports {target!r} — plugins speak only phoebe.api "
                "(plus phoebe.domain data types)")
    return violations


def _class_body_violations(plugin_cls: type) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(plugin_cls)))
    except (OSError, TypeError, SyntaxError):
        return [f"cannot read source of {plugin_cls.__name__!r}"]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr == "sleep") or \
                (isinstance(fn, ast.Name) and fn.id == "sleep"):
            violations.append(
                "calls sleep() — settling belongs in controller options, "
                "waiting belongs in ctx.checkpoint (refactor.md §18)")
    return violations


def _entrypoint_checkpoints(plugin_cls: type, method_name: str) -> bool:
    """True when the entrypoint awaits ctx.checkpoint or delegates to the
    grid_scan primitive (which checkpoints every point)."""
    try:
        source = textwrap.dedent(
            inspect.getsource(getattr(plugin_cls, method_name)))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in ("checkpoint",
                                                             "grid_scan"):
                return True
            if isinstance(fn, ast.Name) and fn.id == "grid_scan":
                return True
    return False


def _signature_violations(spec: PluginSpec) -> list[str]:
    violations: list[str] = []
    fn = getattr(spec.plugin_cls, spec.method_name)
    if not inspect.iscoroutinefunction(fn):
        violations.append(f"entrypoint {spec.method_name!r} is not async")
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:
        hints = {}
    params = [p for p in inspect.signature(fn).parameters.values()
              if p.name != "self"]
    if len(params) < 2:
        return violations + [
            f"entrypoint {spec.method_name!r} must take (config, ctx, ...)"]
    if hints.get(params[0].name) is not spec.config_type:
        violations.append(
            f"first parameter {params[0].name!r} must be annotated with the "
            f"plugin's config type {spec.config_type.__name__}")
    ctx_hint = hints.get(params[1].name)
    if ctx_hint is not RunContext:
        violations.append(
            f"second parameter {params[1].name!r} must be annotated RunContext")
    for param in params[2:]:
        if not isinstance(param.default, Depends):
            violations.append(
                f"device parameter {param.name!r} must default to Depends(...)")
            continue
        hint = hints.get(param.name)
        if hint is None or kind_of_protocol(hint) is None:
            violations.append(
                f"device parameter {param.name!r} must be annotated with a "
                "registered capability protocol")
    return violations


def check_plugin(plugin_id: str, registry: PluginRegistry) -> list[str]:
    """All conformance violations for one registered plugin."""
    violations: list[str] = []
    specs = registry.specs_for_plugin(plugin_id)
    if not specs:
        return [f"plugin {plugin_id!r} is not registered"]

    manifest = registry.manifest(plugin_id)
    if manifest is None:
        violations.append("no manifest")
    else:
        from packaging.version import InvalidVersion, Version
        try:
            Version(manifest.version)
        except InvalidVersion:
            violations.append(f"manifest version {manifest.version!r} does "
                              "not parse (PEP 440)")
        try:
            if not api_compatible(manifest.api):
                violations.append(f"manifest api range {manifest.api!r} "
                                  "excludes this kernel's plugin API")
        except PluginLoadError as exc:
            violations.append(str(exc))
        if set(manifest.commands) != {s.command for s in specs}:
            violations.append("manifest commands drift from registered "
                              "entrypoints")

    classes = {s.plugin_cls for s in specs}
    for cls in classes:
        config_type = cls.config_type
        if not (isinstance(config_type, type)
                and issubclass(config_type, ContractModel)):
            violations.append(f"{cls.__name__}.config_type must subclass "
                              "ContractModel")
        else:
            try:
                config_type.model_json_schema()
            except Exception as exc:
                violations.append(f"config schema does not export: {exc}")
        violations.extend(_class_body_violations(cls))
        module = sys.modules.get(cls.__module__)
        if module is not None:
            violations.extend(_import_violations(module))

    for spec in specs:
        violations.extend(_signature_violations(spec))
        if not _entrypoint_checkpoints(spec.plugin_cls, spec.method_name):
            violations.append(
                f"entrypoint for {spec.command!r} never awaits "
                "ctx.checkpoint (or grid_scan) — the run cannot be paused, "
                "cancelled, or heartbeat-monitored")
    return violations


def check_registry(registry: PluginRegistry) -> dict[str, list[str]]:
    """Conformance report for every plugin in the registry (empty lists =
    conforming)."""
    return {plugin_id: check_plugin(plugin_id, registry)
            for plugin_id in registry.plugin_ids()}
