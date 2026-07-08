"""Experiment plugin layer (refactor.md §11.1).

Plugins are fully independent: no Driver/Controller imports, no UI
references; they subscribe to commands, depend only on capability protocols,
write data through ``ctx.writer`` and observations through ``ctx.emit_*``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Type

from .contracts import ContractModel


class Plugin:
    """Base class; subclasses set ``plugin_id`` / ``config_type`` and mark the
    entrypoint with ``@on_command``."""

    plugin_id: str = ""
    config_type: Type[ContractModel] = ContractModel


def on_command(command: str) -> Callable:
    """Marks a plugin coroutine as the handler for a gateway command."""

    def mark(fn: Callable) -> Callable:
        fn.__phoebe_command__ = command
        return fn

    return mark


@dataclass(frozen=True, slots=True)
class PluginSpec:
    plugin_id: str
    command: str
    plugin_cls: type[Plugin]
    method_name: str
    config_type: type[ContractModel]

    def instantiate(self) -> Plugin:
        return self.plugin_cls()

    def entrypoint(self, instance: Plugin) -> Callable[..., Any]:
        return getattr(instance, self.method_name)


class PluginRegistry:
    def __init__(self) -> None:
        self._by_command: dict[str, PluginSpec] = {}
        self._by_plugin: dict[str, list[PluginSpec]] = {}

    def register_class(self, plugin_cls: type[Plugin], *, plugin_id: str) -> None:
        found = False
        for name in dir(plugin_cls):
            member = getattr(plugin_cls, name)
            command = getattr(member, "__phoebe_command__", None)
            if command is None:
                continue
            found = True
            spec = PluginSpec(
                plugin_id=plugin_id,
                command=command,
                plugin_cls=plugin_cls,
                method_name=name,
                config_type=plugin_cls.config_type,
            )
            if command in self._by_command:
                raise ValueError(
                    f"command {command!r} already registered by "
                    f"{self._by_command[command].plugin_id}"
                )
            self._by_command[command] = spec
            self._by_plugin.setdefault(plugin_id, []).append(spec)
        if not found:
            raise ValueError(
                f"plugin {plugin_id!r} has no @on_command entrypoint"
            )

    def spec_for_command(self, command: str) -> PluginSpec | None:
        return self._by_command.get(command)

    def commands(self) -> tuple[str, ...]:
        return tuple(self._by_command.keys())


#: Default process-wide registry used by the @register decorator.
plugin_registry = PluginRegistry()


def register(*, plugin_id: str,
             registry: PluginRegistry | None = None) -> Callable[[type[Plugin]], type[Plugin]]:
    def deco(cls: type[Plugin]) -> type[Plugin]:
        cls.plugin_id = plugin_id
        (registry or plugin_registry).register_class(cls, plugin_id=plugin_id)
        return cls

    return deco
