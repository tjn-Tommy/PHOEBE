"""Experiment plugin layer (refactor.md §11.1; platform v2 per plan §6.6).

Plugins are fully independent: no Driver/Controller imports, no UI
references; they subscribe to commands, depend only on capability protocols,
write data through ``ctx.writer`` and observations through ``ctx.emit_*``.
Plugin authors import everything from :mod:`phoebe.api` — the only
sanctioned surface (B5).

Platform v2 adds **static manifests** (``PluginManifest``: id/version/PEP 440
API range/commands — facts, never mutable state), **per-plugin failure
records** (A8: one broken plugin degrades visibly, never aborts startup),
**enable/disable without instantiation**, and **directory discovery** of
``plugin.toml``-manifested packages.  Registries are instances —
``build_runtime(plugins=...)`` scopes one per runtime; the module-level
``plugin_registry`` is only the default the ``@register`` decorator targets.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import tomllib
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from loguru import logger

from ..contracts.errors import error_info_of
from ..contracts.plugin import PluginManifest, PluginStatusView
from .contracts import ContractModel, validate_boundary

#: The plugin-facing API version this kernel implements.  Bumped only on
#: breaking changes to the plugin surface (ctx / Depends / capability
#: protocols); admission rejects plugins declaring another major version
#: with PLUGIN_API_INCOMPATIBLE instead of failing mid-run (plan §6.4).
PLUGIN_API_VERSION = 1

MANIFEST_FILENAME = "plugin.toml"
DEFAULT_ENTRY = "plugin.py"


class PluginLoadError(Exception):
    """A plugin package could not be loaded (bad manifest, incompatible API
    range, missing entry) — recorded per plugin, never fatal to startup."""


def api_compatible(specifier: str) -> bool:
    """A9: PEP 440 range in the manifest vs. the kernel's plugin API version."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version

    try:
        return SpecifierSet(specifier).contains(Version(str(PLUGIN_API_VERSION)))
    except InvalidSpecifier as exc:
        raise PluginLoadError(f"invalid api range {specifier!r}: {exc}") from exc


class Plugin:
    """Base class; subclasses set ``plugin_id`` / ``config_type`` and mark the
    entrypoint with ``@on_command``."""

    plugin_id: str = ""
    config_type: type[ContractModel] = ContractModel
    api_version: int = PLUGIN_API_VERSION
    version: str = "0.0.0"               # surfaced in the derived manifest


def on_command(command: str) -> Callable:
    """Marks a plugin coroutine as the handler for a gateway command."""

    def mark(fn: Callable) -> Callable:
        fn.__phoebe_command__ = command
        return fn

    return mark


@dataclass(frozen=True, slots=True)
class PluginSpec:
    plugin_id: str
    command: str
    plugin_cls: type[Plugin]
    method_name: str
    config_type: type[ContractModel]
    api_version: int = PLUGIN_API_VERSION

    def instantiate(self) -> Plugin:
        return self.plugin_cls()

    def entrypoint(self, instance: Plugin) -> Callable[..., Any]:
        return getattr(instance, self.method_name)


def _derive_manifest(plugin_cls: type[Plugin], plugin_id: str,
                     commands: tuple[str, ...]) -> PluginManifest:
    """Builtin plugins get their manifest from the class — same static facts,
    no second source of truth to drift."""
    api_version = getattr(plugin_cls, "api_version", PLUGIN_API_VERSION)
    doc = (inspect.getdoc(plugin_cls) or "").strip().splitlines()
    return PluginManifest(
        plugin_id=plugin_id,
        name=plugin_cls.__name__,
        version=getattr(plugin_cls, "version", "0.0.0"),
        api=f">={api_version},<{api_version + 1}",
        commands=commands,
        requires_hardware=bool(getattr(plugin_cls, "requires_hardware", False)),
        description=doc[0] if doc else "",
    )


class PluginRegistry:
    def __init__(self) -> None:
        self._by_command: dict[str, PluginSpec] = {}
        self._by_plugin: dict[str, list[PluginSpec]] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._sources: dict[str, str] = {}
        self._details: dict[str, str] = {}
        self._disabled: set[str] = set()
        self._failures: list[PluginStatusView] = []

    # ---------------------------------------------------------- registration
    def register_class(self, plugin_cls: type[Plugin], *, plugin_id: str,
                       manifest: PluginManifest | None = None,
                       source: str = "builtin") -> None:
        found: list[str] = []
        for name in dir(plugin_cls):
            member = getattr(plugin_cls, name)
            command = getattr(member, "__phoebe_command__", None)
            if command is None:
                continue
            found.append(command)
            spec = PluginSpec(
                plugin_id=plugin_id,
                command=command,
                plugin_cls=plugin_cls,
                method_name=name,
                config_type=plugin_cls.config_type,
                api_version=getattr(plugin_cls, "api_version", PLUGIN_API_VERSION),
            )
            if command in self._by_command:
                raise ValueError(
                    f"command {command!r} already registered by "
                    f"{self._by_command[command].plugin_id}"
                )
            self._by_command[command] = spec
            self._by_plugin.setdefault(plugin_id, []).append(spec)
        if not found:
            raise ValueError(
                f"plugin {plugin_id!r} has no @on_command entrypoint"
            )
        registered = tuple(s.command for s in self._by_plugin[plugin_id])
        if manifest is None:
            manifest = self._manifests.get(plugin_id) or _derive_manifest(
                plugin_cls, plugin_id, registered)
        # manifest-declared commands are derived facts — consistency-checked
        # against the actual entrypoints, never trusted on their own (§6.6)
        if manifest.commands and set(manifest.commands) != set(registered):
            raise ValueError(
                f"plugin {plugin_id!r}: manifest declares commands "
                f"{sorted(manifest.commands)} but the code registers "
                f"{sorted(registered)}")
        self._manifests[plugin_id] = manifest.model_copy(
            update={"commands": registered})
        self._sources[plugin_id] = source

    def record_failure(self, *, source: str, plugin_id: str,
                       exc: BaseException) -> None:
        """A8: capture error + traceback tail; the platform keeps running."""
        tail = "".join(traceback.format_exception(exc))[-2000:]
        self._failures.append(PluginStatusView(
            plugin_id=plugin_id, state="failed", source=source,
            error=error_info_of(exc), detail=tail))

    def note_detail(self, plugin_id: str, detail: str) -> None:
        self._details[plugin_id] = detail

    # --------------------------------------------------------------- queries
    def spec_for_command(self, command: str) -> PluginSpec | None:
        return self._by_command.get(command)

    def commands(self) -> tuple[str, ...]:
        return tuple(self._by_command.keys())

    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(self._by_plugin.keys())

    def manifest(self, plugin_id: str) -> PluginManifest | None:
        return self._manifests.get(plugin_id)

    def specs_for_plugin(self, plugin_id: str) -> tuple[PluginSpec, ...]:
        return tuple(self._by_plugin.get(plugin_id, ()))

    def failures(self) -> tuple[PluginStatusView, ...]:
        return tuple(self._failures)

    def source_of(self, plugin_id: str) -> str | None:
        return self._sources.get(plugin_id)

    def status(self) -> list[PluginStatusView]:
        """The availability report: every known plugin, loaded/disabled/failed."""
        rows = []
        for plugin_id, manifest in self._manifests.items():
            rows.append(PluginStatusView(
                plugin_id=plugin_id,
                state="disabled" if plugin_id in self._disabled else "loaded",
                version=manifest.version, api=manifest.api,
                commands=manifest.commands,
                source=self._sources.get(plugin_id, "builtin"),
                detail=self._details.get(plugin_id),
            ))
        rows.extend(self._failures)
        return rows

    # -------------------------------------------------------- enable/disable
    def is_disabled(self, plugin_id: str) -> bool:
        return plugin_id in self._disabled

    def disable(self, plugin_id: str) -> None:
        if plugin_id not in self._by_plugin:
            raise KeyError(plugin_id)
        self._disabled.add(plugin_id)

    def enable(self, plugin_id: str) -> None:
        if plugin_id not in self._by_plugin:
            raise KeyError(plugin_id)
        self._disabled.discard(plugin_id)


#: Default process-wide registry used by the @register decorator.
plugin_registry = PluginRegistry()


def register(*, plugin_id: str,
             registry: PluginRegistry | None = None) -> Callable[[type[Plugin]], type[Plugin]]:
    def deco(cls: type[Plugin]) -> type[Plugin]:
        cls.plugin_id = plugin_id
        (registry or plugin_registry).register_class(cls, plugin_id=plugin_id)
        return cls

    return deco


# ------------------------------------------------------------------ discovery

def _import_plugin_module(path: Path, plugin_id: str) -> Any:
    if not path.is_file():
        raise PluginLoadError(f"entry module {path.name!r} not found")
    name = "phoebe_ext." + re.sub(r"[^0-9A-Za-z_]", "_", plugin_id)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot import entry module {path.name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module           # so get_type_hints resolves
    spec.loader.exec_module(module)
    return module


def load_plugin_directory(root: str | Path,
                          registry: PluginRegistry | None = None,
                          *,
                          package: str | None = None,
                          source: str | None = None,
                          ) -> list[PluginStatusView]:
    """Discover manifested plugin packages (plan §6.6): every subdirectory of
    ``root`` holding a ``plugin.toml``.  Each package loads independently —
    a broken one yields a failure record and a log line, never an aborted
    startup (A8).  A ``requirements.txt`` is surfaced as a report; nothing is
    ever pip-installed at runtime.  Returns the resulting status rows.

    ``package`` imports each entry module as a real submodule of that python
    package (used for the builtin plugins shipped inside ``phoebe.plugins``,
    so their identity and relative imports stay ordinary); without it the
    entry file loads standalone under the ``phoebe_ext.*`` namespace.
    ``source`` overrides the recorded provenance (default: the folder path).
    Loading the same plugin from the same source twice is a no-op, so
    repeated calls are idempotent."""
    reg = registry or plugin_registry
    root = Path(root)
    if not root.is_dir():
        return []
    seen: list[str] = []
    for child in sorted(root.iterdir()):
        manifest_path = child / MANIFEST_FILENAME
        if not child.is_dir() or not manifest_path.is_file():
            continue
        plugin_id = child.name           # best guess until the manifest parses
        src = source or str(child)
        try:
            raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = validate_boundary(PluginManifest, raw)
            plugin_id = manifest.plugin_id
            if reg.manifest(plugin_id) is not None:
                if reg.source_of(plugin_id) == src:
                    continue             # already loaded from here — idempotent
                raise PluginLoadError(
                    f"plugin {plugin_id!r} is already registered from "
                    f"{reg.source_of(plugin_id)!r}")
            if not api_compatible(manifest.api):
                raise PluginLoadError(
                    f"plugin {manifest.plugin_id!r} requires plugin API "
                    f"{manifest.api!r}; this kernel implements "
                    f"{PLUGIN_API_VERSION}")
            entry = manifest.entry or DEFAULT_ENTRY
            if package is not None:
                module = importlib.import_module(
                    f"{package}.{child.name}.{Path(entry).stem}")
            else:
                module = _import_plugin_module(child / entry,
                                               manifest.plugin_id)
            classes = [obj for obj in vars(module).values()
                       if isinstance(obj, type) and issubclass(obj, Plugin)
                       and obj is not Plugin
                       and obj.__module__ == module.__name__]
            if not classes:
                raise PluginLoadError("entry module defines no Plugin subclass")
            for cls in classes:
                reg.register_class(cls, plugin_id=manifest.plugin_id,
                                   manifest=manifest, source=src)
            requirements = child / "requirements.txt"
            if requirements.is_file():
                lines = [ln for ln in requirements.read_text(encoding="utf-8")
                         .splitlines() if ln.strip() and not ln.startswith("#")]
                reg.note_detail(manifest.plugin_id,
                                f"requirements.txt: {len(lines)} entries — "
                                "install out-of-process, never at runtime")
        except Exception as exc:
            logger.opt(exception=True).error(
                "plugin {} failed to load from {} — platform continues degraded",
                plugin_id, child)
            reg.record_failure(source=src, plugin_id=plugin_id, exc=exc)
        else:
            seen.append(plugin_id)
    if seen:
        logger.info("loaded {} plugin package(s) from {}: {}",
                    len(seen), root, ", ".join(seen))
    return reg.status()
