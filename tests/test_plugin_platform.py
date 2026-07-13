"""Plugin platform v2 (plan §6.6, PR D-1): manifests, directory discovery
with per-plugin failure records, enable/disable, PLUGIN_DISABLED admission."""
from __future__ import annotations

import textwrap
import uuid

import pytest

from phoebe.app.bootstrap import build_runtime
from phoebe.contracts.commands import AckCode, CommandEnvelope
from phoebe.contracts.plugin import manifest_hash
from phoebe.core.config import parse_app_config
from phoebe.core.plugin import PluginRegistry, load_plugin_directory, plugin_registry
from phoebe.plugins import load_builtin_plugins
from phoebe.plugins.spectrum_grid import SpectrumGridPlugin
from phoebe.plugins.tpa_multiplier import TPAMultiplierPlugin

load_builtin_plugins()

SLM_H, SLM_W = 60, 80


def _sim_config(runs_root: str, plugin_dirs: tuple[str, ...] = ()) -> dict:
    return {
        "mode": "dev",
        "storage": {"runs_root": runs_root},
        "plugin_dirs": list(plugin_dirs),
        "instruments": [
            {"instrument_id": "slm.primary", "kind": "pattern_modulator",
             "vendor": "santec", "model": "slm-200", "role": "primary_slm",
             "backend": "sim",
             "connection": {"transport": "vendor_dll", "dll_path": "unused"},
             "options": {"settle_ms": 1.0, "height": SLM_H, "width": SLM_W,
                         "levels": 1024, "lut_id": "sim_lut"}},
            {"instrument_id": "osa.main", "kind": "spectrum_analyzer",
             "vendor": "yokogawa", "model": "aq6370", "role": "main_osa",
             "backend": "sim",
             "connection": {"transport": "tcp", "host": "sim", "port": 10001}},
        ],
        "plugins": {
            "org.lab.tpa_multiplier": {"bindings": {"slm": "primary_slm",
                                                    "osa": "main_osa"}},
        },
    }


GOOD_MANIFEST = """
plugin_id = "org.demo.good"
name = "Good demo"
version = "1.2.3"
api = ">=1,<2"
commands = ["demo_good_run"]
"""

GOOD_PLUGIN = '''
from phoebe.api import (ContractModel, Depends, Plugin, RunContext,
                        SpectrumAnalyzer, on_command)


class GoodConfig(ContractModel):
    steps: int = 3


class GoodPlugin(Plugin):
    """A conforming manifested demo plugin."""
    config_type = GoodConfig
    version = "1.2.3"

    @on_command("demo_good_run")
    async def run(self, config: GoodConfig, ctx: RunContext,
                  osa: SpectrumAnalyzer = Depends(role="main_osa")) -> None:
        for step in range(config.steps):
            await ctx.checkpoint("demo", step=step)
'''


def _write_plugin(root, dirname, manifest: str, code: str,
                  entry: str = "plugin.py"):
    pdir = root / dirname
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(textwrap.dedent(manifest),
                                      encoding="utf-8")
    (pdir / entry).write_text(textwrap.dedent(code), encoding="utf-8")
    return pdir


# ------------------------------------------------------------- D-1 manifests
def test_builtin_plugins_are_manifested():
    """Acceptance: builtin plugins carry derived manifests — same facts as
    the code, no second source of truth."""
    for plugin_id, command in (("org.lab.tpa_multiplier", "start_tpa_run"),
                               ("org.lab.spectrum_grid", "start_grid_scan")):
        manifest = plugin_registry.manifest(plugin_id)
        assert manifest is not None
        assert manifest.commands == (command,)
        assert manifest.api == ">=1,<2"
        assert len(manifest_hash(manifest)) == 16
    states = {row.plugin_id: row.state for row in plugin_registry.status()}
    assert states["org.lab.tpa_multiplier"] == "loaded"


def test_manifest_command_drift_is_rejected():
    """§6.6: manifest-declared commands are consistency-checked against the
    actual entrypoints at registration."""
    from phoebe.contracts.plugin import PluginManifest

    registry = PluginRegistry()
    lying = PluginManifest(plugin_id="org.lab.tpa_multiplier",
                           commands=("some_other_command",))
    with pytest.raises(ValueError, match="drift|declares"):
        registry.register_class(TPAMultiplierPlugin,
                                plugin_id="org.lab.tpa_multiplier",
                                manifest=lying)


# ------------------------------------------------------------ D-1 discovery
def test_directory_discovery_degrades_not_aborts(tmp_path):
    """Acceptance: a broken plugin yields a failure record; the good one
    loads; startup never aborts (A8)."""
    root = tmp_path / "plugins"
    _write_plugin(root, "good", GOOD_MANIFEST, GOOD_PLUGIN)
    _write_plugin(root, "broken", """
        plugin_id = "org.demo.broken"
        api = ">=1,<2"
        """, "import nonexistent_module_xyz_42\n")
    _write_plugin(root, "oldapi", """
        plugin_id = "org.demo.oldapi"
        api = ">=99,<100"
        """, GOOD_PLUGIN)

    registry = PluginRegistry()
    rows = load_plugin_directory(root, registry)

    assert "demo_good_run" in registry.commands()
    states = {row.plugin_id: row for row in rows}
    assert states["org.demo.good"].state == "loaded"
    assert states["org.demo.good"].version == "1.2.3"
    assert states["org.demo.broken"].state == "failed"
    assert "nonexistent_module_xyz_42" in states["org.demo.broken"].error.message
    assert states["org.demo.oldapi"].state == "failed"
    assert "plugin API" in states["org.demo.oldapi"].error.message


def test_discovery_surfaces_requirements_txt(tmp_path):
    root = tmp_path / "plugins"
    pdir = _write_plugin(root, "good", GOOD_MANIFEST, GOOD_PLUGIN)
    (pdir / "requirements.txt").write_text("scipy>=1.10\n", encoding="utf-8")
    registry = PluginRegistry()
    rows = load_plugin_directory(root, registry)
    good = next(r for r in rows if r.plugin_id == "org.demo.good")
    assert "requirements.txt" in (good.detail or "")
    assert "out-of-process" in good.detail


async def test_bootstrap_loads_plugin_dirs_and_runs_them(tmp_path):
    """End to end: config.plugin_dirs → discovery → dispatch through the
    full sim runtime, scoped to an instance registry."""
    root = tmp_path / "plugins"
    _write_plugin(root, "good", GOOD_MANIFEST, GOOD_PLUGIN)
    _write_plugin(root, "broken", """
        plugin_id = "org.demo.broken"
        api = ">=1,<2"
        """, "raise RuntimeError('boom at import')\n")

    registry = PluginRegistry()
    registry.register_class(TPAMultiplierPlugin,
                            plugin_id="org.lab.tpa_multiplier")
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs"),
                                       plugin_dirs=(str(root),)))
    rt = await build_runtime(cfg, plugins=registry,
                             runs_root=tmp_path / "runs", start_reaper=False)
    try:
        assert registry.failures()[0].plugin_id == "org.demo.broken"
        ack = await rt.services.runs.submit(CommandEnvelope(
            command_id=f"cmd-{uuid.uuid4().hex[:8]}",
            command="demo_good_run", payload={"steps": 2}))
        assert ack.accepted, ack.reason
        await rt.task_manager.wait(ack.task_id)
    finally:
        await rt.shutdown()


# ------------------------------------------------------- D-1 enable/disable
async def test_disable_rejects_with_typed_code_and_enable_restores(tmp_path):
    registry = PluginRegistry()
    registry.register_class(TPAMultiplierPlugin,
                            plugin_id="org.lab.tpa_multiplier")
    registry.register_class(SpectrumGridPlugin,
                            plugin_id="org.lab.spectrum_grid")
    cfg = parse_app_config(_sim_config(str(tmp_path / "runs")))
    rt = await build_runtime(cfg, plugins=registry,
                             runs_root=tmp_path / "runs", start_reaper=False)
    try:
        def envelope():
            return CommandEnvelope(
                command_id=f"cmd-{uuid.uuid4().hex[:8]}",
                command="start_tpa_run",
                payload={"max_steps": 2, "seed": 1,
                         "scan": {"center_nm": 778.0, "span_nm": 8.0,
                                  "points": 101}})

        await rt.services.plugins.disable("org.lab.tpa_multiplier")
        ack = await rt.services.runs.submit(envelope())
        assert not ack.accepted
        assert ack.code is AckCode.PLUGIN_DISABLED

        status = {r.plugin_id: r.state
                  for r in await rt.services.plugins.status()}
        assert status["org.lab.tpa_multiplier"] == "disabled"
        assert status["org.lab.spectrum_grid"] == "loaded"

        await rt.services.plugins.enable("org.lab.tpa_multiplier")
        ack = await rt.services.runs.submit(envelope())
        assert ack.accepted
        await rt.task_manager.wait(ack.task_id)
    finally:
        await rt.shutdown()


def test_enable_disable_unknown_plugin_raises():
    registry = PluginRegistry()
    with pytest.raises(KeyError):
        registry.disable("org.unknown")
    with pytest.raises(KeyError):
        registry.enable("org.unknown")
