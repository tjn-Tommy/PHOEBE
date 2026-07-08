"""Controller factory registry — the composition root's building block
(refactor.md §5.1–§5.2).

Three registries stay strictly separate: this one (which class to build for a
configured kind+vendor+model), DeviceManager's instance registry (which live
controller answers to a logical id), and each controller's CapabilityRegistry
(what this concrete unit provides).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .config import AppConfig, InstrumentConfig
from .controller import InstrumentController
from .errors import UnsupportedInstrumentModelError
from .worker import WorkerPool

if TYPE_CHECKING:
    from .bus import EventBus


@dataclass(frozen=True, slots=True)
class ControllerKey:
    kind: str
    vendor: str
    model: str


@dataclass
class AppDependencies:
    """Injected services available to controller factories."""

    worker_pool: WorkerPool
    bus: "EventBus | None" = None
    app_config: AppConfig | None = None
    sim_contexts: dict[str, object] = field(default_factory=dict)

    def sim_context(self, key: str, factory: Callable[[], object]) -> object:
        """Shared per-run-process simulation state (refactor.md §14.2)."""
        if key not in self.sim_contexts:
            self.sim_contexts[key] = factory()
        return self.sim_contexts[key]


ControllerFactory = Callable[[InstrumentConfig, AppDependencies], InstrumentController]


class ControllerFactoryRegistry:
    def __init__(self) -> None:
        self._factories: dict[ControllerKey, ControllerFactory] = {}
        self._sim_factories: dict[str, ControllerFactory] = {}   # by kind

    def register(self, key: ControllerKey, factory: ControllerFactory) -> None:
        if key in self._factories:
            raise ValueError(f"Duplicate controller key: {key}")
        self._factories[key] = factory

    def register_sim(self, kind: str, factory: ControllerFactory) -> None:
        """Simulation backend for a capability kind (backend = \"sim\")."""
        if kind in self._sim_factories:
            raise ValueError(f"Duplicate sim factory for kind: {kind}")
        self._sim_factories[kind] = factory

    def create(self, cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
        if cfg.backend == "sim":
            factory = self._sim_factories.get(cfg.kind)
            if factory is None:
                raise UnsupportedInstrumentModelError(
                    f"no sim backend registered for kind {cfg.kind!r} "
                    f"({cfg.instrument_id})"
                )
            return factory(cfg, deps)
        key = ControllerKey(cfg.kind, cfg.vendor, cfg.model)
        try:
            factory = self._factories[key]
        except KeyError as exc:
            raise UnsupportedInstrumentModelError(
                f"no controller factory for {key} ({cfg.instrument_id})"
            ) from exc
        return factory(cfg, deps)
