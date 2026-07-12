"""Startup configuration: parsed once, fail fast (refactor.md §3.5, §5.3).

TOML is parsed into a strongly-typed ``AppConfig`` before any hardware is
touched; misspelled keys, out-of-range values and missing fields all explode
at startup.  ``InstrumentConfig.options`` stays generic here — the concrete
controller factory re-validates it against the model's own Options contract
(two-stage validation, still strict).
"""
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..contracts.api import ServerRole
from .contracts import ContractModel, InstrumentId, Seconds, validate_boundary
from .errors import PhoebeConfigError


class VisaConnection(ContractModel):
    transport: Literal["visa"] = "visa"
    resource_name: str
    timeout_s: Seconds = 10.0
    visa_library: str = ""       # "" = system default, "@py" = pyvisa-py


class TcpConnection(ContractModel):
    transport: Literal["tcp"] = "tcp"
    host: str
    port: Annotated[int, Field(ge=1, le=65_535)]
    timeout_s: Seconds = 10.0


class DllConnection(ContractModel):
    transport: Literal["vendor_dll"] = "vendor_dll"
    dll_path: str
    device_index: Annotated[int, Field(ge=0)] = 0


class SdkConnection(ContractModel):
    """Vendor SDK addressed by a device name (e.g. NI-DAQmx \"Dev1\")."""

    transport: Literal["sdk"] = "sdk"
    device: str
    timeout_s: Seconds = 30.0


Connection = Annotated[
    VisaConnection | TcpConnection | DllConnection | SdkConnection,
    Field(discriminator="transport"),
]


class InstrumentConfig(ContractModel):
    instrument_id: InstrumentId
    kind: str                    # "spectrum_analyzer" / "pattern_modulator" / ...
    vendor: str
    model: str
    role: str                    # DI binding name (§7), e.g. "main_osa"
    backend: Literal["real", "sim"] = "real"
    connection: Connection
    options: dict[str, object] = Field(default_factory=dict)


class SuspenderConfig(ContractModel):
    """Auto-suspend rule: metric out of range → pause; back in range → resume."""

    watch_topic: Literal["device_health", "progress"]
    metric: str                  # e.g. "pump_power_mw"
    min_value: float | None = None
    max_value: float | None = None
    grace_s: Seconds = 5.0


class StorageConfig(ContractModel):
    runs_root: str = "runs"
    writer_queue_size: Annotated[int, Field(ge=4, le=4096)] = 64
    compact_metrics_to_parquet: bool = True


class ReconnectSettings(ContractModel):
    """Device reconnect / lifecycle policy (plan §6.3): exponential backoff
    with a give-up ceiling, threshold-triggered handle rebuild, bounded
    health probes."""

    base_delay_s: Seconds = 1.0
    max_delay_s: Seconds = 60.0
    multiplier: Annotated[float, Field(ge=1.0)] = 2.0
    give_up_attempts: Annotated[int, Field(ge=0)] = 10       # 0 = never give up
    rebuild_after_probe_failures: Annotated[int, Field(ge=1)] = 3
    probe_timeout_s: Seconds = 10.0


class ServerConfig(ContractModel):
    """``[server]`` — the HTTP adapter (plan Phase E).  Fail-closed exposure
    ladder (lessons §8.2): a non-loopback ``host`` requires an *explicit*
    ``token`` and ``role = "read_only"`` — ``phoebe.server.auth`` refuses to
    start otherwise.  An empty token means one is generated per process
    start (localhost convenience)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65_535)] = 8760
    token: str = ""                      # "" → per-process generated token
    role: ServerRole = "operator"
    sse_keepalive_s: Seconds = 15.0      # SSE comment ping interval


class AppConfig(ContractModel):
    instruments: tuple[InstrumentConfig, ...]
    plugin_bindings: dict[str, dict[str, str]] = Field(default_factory=dict)
    dispatch_policy: Literal["reject", "queue"] = "reject"
    bus_default_queue_size: Annotated[int, Field(ge=16, le=4096)] = 256
    mode: Literal["dev", "prod"] = "dev"     # dev asserts payload sizes, validates responses
    storage: StorageConfig = StorageConfig()
    suspenders: tuple[SuspenderConfig, ...] = ()
    lease_ttl_s: Seconds = 600.0
    cleanup_timeout_s: Seconds = 30.0
    reconnect: ReconnectSettings = ReconnectSettings()
    health_poll_interval_s: Annotated[float, Field(ge=0)] = 30.0   # 0 disables the poller
    server: ServerConfig = ServerConfig()

    def instrument(self, instrument_id: str) -> InstrumentConfig:
        for cfg in self.instruments:
            if cfg.instrument_id == instrument_id:
                return cfg
        raise KeyError(instrument_id)

    def role_map(self) -> dict[str, InstrumentId]:
        mapping: dict[str, InstrumentId] = {}
        for cfg in self.instruments:
            if cfg.role in mapping:
                raise PhoebeConfigError(f"duplicate role {cfg.role!r} in instrument config")
            mapping[cfg.role] = cfg.instrument_id
        return mapping


def load_app_config(path: str | Path) -> AppConfig:
    """Parse a TOML file into an AppConfig; raise PhoebeConfigError on any problem."""
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PhoebeConfigError(f"cannot read config {p}: {exc}") from exc
    return parse_app_config(raw, source=str(p))


def parse_app_config(raw: dict, *, source: str = "<dict>") -> AppConfig:
    # TOML represents the plugin binding tables as [plugins."<id>".bindings]
    plugins = raw.pop("plugins", {})
    bindings: dict[str, dict[str, str]] = dict(raw.get("plugin_bindings", {}))
    for plugin_id, table in plugins.items():
        if "bindings" in table:
            bindings[plugin_id] = dict(table["bindings"])
    raw["plugin_bindings"] = bindings
    try:
        cfg = validate_boundary(AppConfig, raw)
    except ValidationError as exc:
        raise PhoebeConfigError(f"invalid config in {source}:\n{exc}") from exc
    cfg.role_map()  # surface duplicate roles at startup
    return cfg


def config_hash(cfg: AppConfig) -> str:
    """Stable hash of the full config, recorded in every RunManifest."""
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]
