"""Experiment plugins. Importing a module registers its commands.

Import the modules you deploy explicitly (or via ``load_builtin_plugins``)
so plugin registration is a visible, auditable act.
"""


def load_builtin_plugins() -> None:
    from . import tpa_multiplier  # noqa: F401
    from . import spectrum_grid   # noqa: F401
