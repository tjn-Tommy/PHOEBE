"""First-party experiment plugins.

Every builtin plugin is a self-contained folder in the exact same format
third-party ``plugin_dirs`` entries use — a ``plugin.toml`` manifest next to
the ``plugin.py`` code (which imports only :mod:`phoebe.api` plus
``phoebe.domain`` data types) — and loads through the same discovery path,
so the builtins permanently dogfood the third-party contract.  Importing a
plugin package has no side effects; registration happens here.
"""
from __future__ import annotations

from pathlib import Path

from ..api import PluginRegistry, PluginStatusView, load_plugin_directory


def load_builtin_plugins(
        registry: PluginRegistry | None = None) -> list[PluginStatusView]:
    """Register the builtin plugin folders (idempotent per registry)."""
    return load_plugin_directory(Path(__file__).parent, registry,
                                 package=__name__, source="builtin")
