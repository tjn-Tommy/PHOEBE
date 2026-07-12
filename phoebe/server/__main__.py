"""``python -m phoebe.server --config config/sim.toml`` — host the HTTP
adapter over a full runtime.

Thread topology mirrors the PyQt deployment (refactor.md §12.1): the phoebe
core owns its dedicated ``LoopThread``; uvicorn runs its own loop on the main
thread; every request marshals through ``ServiceHub.call``.  The security
posture is resolved (fail-closed) *before* any device is touched.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from ..app.bootstrap import LoopThread, build_runtime
from ..core.config import load_app_config
from ..plugins import load_builtin_plugins
from .app import create_app
from .auth import resolve_security


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PHOEBE HTTP adapter (FastAPI over the service layer)")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default=None,
                        help="override [server].host from the config")
    parser.add_argument("--port", type=int, default=None,
                        help="override [server].port from the config")
    args = parser.parse_args(argv)

    import uvicorn

    config = load_app_config(args.config)
    server_cfg = config.server
    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if overrides:
        server_cfg = server_cfg.model_copy(update=overrides)
        config = config.model_copy(update={"server": server_cfg})

    # ladder check first — refuse a bad posture before any device connects
    security = resolve_security(server_cfg)

    load_builtin_plugins()
    loop_thread = LoopThread()
    loop_thread.start()
    runtime = loop_thread.run_coroutine(build_runtime(config)).result()
    app = create_app(runtime.services, security=security)

    if security.generated:
        # print (never log): the log bridge would broadcast it on the bus
        print(f"session token (this process only): {security.token}", flush=True)
    print(f"PHOEBE server on http://{server_cfg.host}:{server_cfg.port}/ "
          f"(role: {security.role}) — API at /api/v1, UI at /ui/", flush=True)

    try:
        uvicorn.run(app, host=server_cfg.host, port=server_cfg.port,
                    log_level="info")
        return 0
    finally:
        logger.info("server stopped — draining runtime")
        loop_thread.run_coroutine(runtime.shutdown()).result(timeout=60)
        loop_thread.stop()


if __name__ == "__main__":
    raise SystemExit(main())
