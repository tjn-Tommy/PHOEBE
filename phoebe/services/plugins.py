"""Plugin service: command inventory + config schemas for form builders.

``config_schema()`` is the single source of defaults/ranges/validation for
UI forms (plan §6.6): panels may be hand-built, but their constraints come
from the plugin's own pydantic schema, never duplicated by hand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.plugin import PluginRegistry, PluginSpec


class PluginService:
    def __init__(self, *, registry: PluginRegistry) -> None:
        self._registry = registry

    async def commands(self) -> tuple[str, ...]:
        return self._registry.commands()

    async def spec_for_command(self, command: str) -> PluginSpec | None:
        return self._registry.spec_for_command(command)

    async def config_schema(self, command: str) -> dict[str, Any] | None:
        """JSON Schema of the command's config model (form metadata)."""
        spec = self._registry.spec_for_command(command)
        if spec is None:
            return None
        return spec.config_type.model_json_schema()
