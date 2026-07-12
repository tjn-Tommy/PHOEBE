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
from ..app.single_instance import AnotherInstanceRunningError, SingleInstanceLock
from ..core.config import load_app_config
from ..core.diagnostics import EventLoopDiagnostics
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

    # one process per deployment: two instances would fight over VISA/DLL
    # handles on real hardware (plan §3.1 A7)
    lock = SingleInstanceLock(Path(config.storage.runs_root) / ".phoebe.lock")
    try:
        lock.acquire()
    except AnotherInstanceRunningError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    # core first: if a device fails identity verification we abort before Qt
    loop_thread = LoopThread()
    loop_thread.start()
    diagnostics = EventLoopDiagnostics(loop_thread.loop)
    try:
        runtime = loop_thread.run_coroutine(build_runtime(config)).result(timeout=120)
        diagnostics.start()
    except Exception:
        loop_thread.stop()
        lock.release()
        raise

    try:
        app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
        services = runtime.services
        bridge = UiEventBridge()
        bridge.start(services)
        window = MainWindow(config, services, bridge)
        window.show()

        # trigger an initial health sweep so the device table fills immediately
        services.call(services.devices.health_check_all())

        exit_code = app.exec()

        bridge.stop()
        loop_thread.run_coroutine(runtime.shutdown()).result(timeout=30)
        diagnostics.stop()
        loop_thread.stop()
        return exit_code
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
