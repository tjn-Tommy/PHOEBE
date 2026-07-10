"""PyQt5 application entry point.

Thread topology per refactor.md §12.1: the Qt main thread runs only the UI;
phoebe's entire core lives on a dedicated asyncio loop thread; the two meet
exclusively at ``gateway.submit_threadsafe`` (in) and ``UiEventBridge`` (out).

Usage::

    python -m phoebe.ui.app --config config/sim.toml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from ..app.bootstrap import LoopThread, build_runtime
from ..core.config import load_app_config
from ..plugins import load_builtin_plugins
from .bridge import UiEventBridge
from .main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PHOEBE experiment control UI")
    parser.add_argument("--config", type=Path,
                        default=Path("config") / "sim.toml",
                        help="TOML instrument configuration (default: config/sim.toml)")
    args = parser.parse_args(argv)

    load_builtin_plugins()
    config = load_app_config(args.config)

    # core first: if a device fails identity verification we abort before Qt
    loop_thread = LoopThread()
    loop_thread.start()
    try:
        runtime = loop_thread.run_coroutine(build_runtime(config)).result(timeout=120)
    except Exception:
        loop_thread.stop()
        raise

    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    bridge = UiEventBridge()
    bridge.start(runtime.bus, loop_thread.loop)
    window = MainWindow(config, runtime.gateway, loop_thread.loop, bridge)
    window.show()

    # trigger an initial health sweep so the device table fills immediately
    loop_thread.run_coroutine(runtime.device_manager.health_check_all())

    exit_code = app.exec()

    bridge.stop()
    loop_thread.run_coroutine(runtime.shutdown()).result(timeout=30)
    loop_thread.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
