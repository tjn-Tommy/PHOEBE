"""Dependency injection: signature parsing and multi-device disambiguation
(refactor.md §7).

Experiment code only declares the capability protocol it needs (plus an
optional role); it never acquires locks.  Binding priority per parameter:

1. explicit ``Depends(role=...)`` on the parameter;
2. the plugin's config binding table (param name → role);
3. unique-by-kind in the inventory;
4. otherwise: fail fast at dispatch.

Capability-protocol → kind mapping is an explicit registration table — no
runtime ``isinstance`` sniffing of Protocols (structural typing checks method
names, not physical semantics).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from .contracts import InstrumentId
from .errors import PhoebeConfigError

# Explicit protocol → kind table, filled by phoebe.instruments.protocols.
_KIND_BY_PROTOCOL: dict[type, str] = {}


def register_capability_kind(protocol: type, kind: str) -> None:
    _KIND_BY_PROTOCOL[protocol] = kind


def kind_of_protocol(protocol: type) -> str | None:
    return _KIND_BY_PROTOCOL.get(protocol)


class Depends:
    """Declares an injected capability parameter, optionally bound to a role."""

    def __init__(self, role: str | None = None) -> None:
        self.role = role

    def __repr__(self) -> str:
        return f"Depends(role={self.role!r})"


@dataclass(frozen=True, slots=True)
class ResolvedRequirement:
    param_name: str
    instrument_id: InstrumentId
    kind: str


class DependencyResolver:
    """Resolves a plugin entrypoint's Depends parameters to instrument ids."""

    def __init__(
        self,
        *,
        role_map: dict[str, InstrumentId],
        kind_index: dict[str, tuple[InstrumentId, ...]],
        plugin_bindings: dict[str, dict[str, str]],
    ) -> None:
        self._role_map = role_map                  # role → instrument_id
        self._kind_index = kind_index              # kind → instrument_ids
        self._plugin_bindings = plugin_bindings    # plugin_id → {param → role}

    def resolve(self, plugin_id: str, fn: Callable[..., Any]) -> list[ResolvedRequirement]:
        signature = inspect.signature(fn)
        try:
            hints = get_type_hints(fn)
        except Exception:            # forward refs pointing at optional deps
            hints = {}
        bindings = self._plugin_bindings.get(plugin_id, {})
        requirements: list[ResolvedRequirement] = []

        for name, param in signature.parameters.items():
            default = param.default
            if not isinstance(default, Depends):
                continue
            annotation = hints.get(name, param.annotation)
            kind = _KIND_BY_PROTOCOL.get(annotation)
            if kind is None:
                raise PhoebeConfigError(
                    f"{plugin_id}: parameter {name!r} is Depends() but its annotation "
                    f"{annotation!r} is not a registered capability protocol"
                )
            role = default.role or bindings.get(name)
            if role is not None:
                instrument_id = self._role_map.get(role)
                if instrument_id is None:
                    raise PhoebeConfigError(
                        f"{plugin_id}: parameter {name!r} wants role {role!r} "
                        f"but no configured instrument has that role"
                    )
            else:
                candidates = self._kind_index.get(kind, ())
                if len(candidates) == 1:
                    instrument_id = candidates[0]
                elif not candidates:
                    raise PhoebeConfigError(
                        f"{plugin_id}: no configured instrument provides {kind!r} "
                        f"needed by parameter {name!r}"
                    )
                else:
                    raise PhoebeConfigError(
                        f"{plugin_id}: {kind!r} is ambiguous ({len(candidates)} devices); "
                        f"bind parameter {name!r} via Depends(role=...) or "
                        f"[plugins.\"{plugin_id}\".bindings]"
                    )
            requirements.append(ResolvedRequirement(name, instrument_id, kind))
        return requirements
