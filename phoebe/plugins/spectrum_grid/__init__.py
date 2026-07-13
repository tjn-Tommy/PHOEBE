"""Folder-format plugin package; the code lives in ``plugin.py`` next to the
``plugin.toml`` manifest.  Importing this package has no side effects —
registration is :func:`phoebe.plugins.load_builtin_plugins`'s job."""
from .plugin import GridScanConfig, SpectrumGridPlugin

__all__ = ["GridScanConfig", "SpectrumGridPlugin"]
