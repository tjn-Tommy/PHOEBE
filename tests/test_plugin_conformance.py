"""Plugin conformance suite (plan §6.6, PR D-3) — runs in CI for builtins.

The static half of "a plugin behaves on this platform": manifest facts,
config schema export, entrypoint shape, zero locks/sleeps, checkpointing,
and B5 import discipline.  The behavioral half (pause/cancel/cleanup under
sim) lives in the e2e suites (test_e2e_sim / test_failure_paths)."""
from __future__ import annotations

import textwrap

from phoebe.core.conformance import check_plugin, check_registry
from phoebe.core.plugin import PluginRegistry, load_plugin_directory, plugin_registry
from phoebe.plugins import load_builtin_plugins

load_builtin_plugins()

BUILTINS = ("org.lab.tpa_multiplier", "org.lab.spectrum_grid")


def test_builtin_plugins_conform():
    """Acceptance (D-3): the conformance suite is green for every builtin."""
    for plugin_id in BUILTINS:
        violations = check_plugin(plugin_id, plugin_registry)
        assert violations == [], f"{plugin_id}: {violations}"


def test_unregistered_plugin_reports():
    assert check_registry(PluginRegistry()) == {}
    assert check_plugin("org.nope", PluginRegistry()) == \
        ["plugin 'org.nope' is not registered"]


BAD_MANIFEST = """
plugin_id = "org.demo.bad"
version = "not-a-version"
api = ">=1,<2"
"""

BAD_PLUGIN = '''
import time

from phoebe.core.plugin import Plugin, on_command      # forbidden import
from phoebe.api import ContractModel, RunContext


class BadConfig(ContractModel):
    n: int = 1


class BadPlugin(Plugin):
    """Deliberately non-conforming fixture."""
    config_type = BadConfig

    @on_command("demo_bad_run")
    async def run(self, config: BadConfig, ctx: RunContext, osa=None) -> None:
        time.sleep(0.001)
'''


def test_nonconforming_plugin_is_fully_diagnosed(tmp_path):
    """Every rule fires: bad version, core import, sleep call, non-Depends
    device parameter, and no checkpoint."""
    pdir = tmp_path / "plugins" / "bad"
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(textwrap.dedent(BAD_MANIFEST),
                                      encoding="utf-8")
    (pdir / "plugin.py").write_text(textwrap.dedent(BAD_PLUGIN),
                                    encoding="utf-8")
    registry = PluginRegistry()
    load_plugin_directory(tmp_path / "plugins", registry)
    assert "demo_bad_run" in registry.commands()   # it loads; conformance flags

    violations = "\n".join(check_plugin("org.demo.bad", registry))
    assert "does not parse" in violations          # PEP 440 version
    assert "phoebe.core.plugin" in violations      # B5 import discipline
    assert "sleep" in violations                   # zero manual sleeps
    assert "Depends" in violations                 # device param shape
    assert "checkpoint" in violations              # pausable/cancellable


def test_conformance_checks_signature_shape():
    """An entrypoint missing (config, ctx) or non-async is flagged."""
    from phoebe.api import ContractModel, Plugin, on_command

    class WeirdConfig(ContractModel):
        pass

    class Weird(Plugin):
        config_type = WeirdConfig

        @on_command("demo_weird_run")
        async def run(self, config) -> None:       # no ctx
            pass

    registry = PluginRegistry()
    registry.register_class(Weird, plugin_id="org.demo.weird")
    violations = "\n".join(check_plugin("org.demo.weird", registry))
    assert "(config, ctx" in violations
