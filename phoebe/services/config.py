"""Config service: read-only access to the validated app configuration."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.config import AppConfig, InstrumentConfig


class ConfigService:
    def __init__(self, *, app_config: AppConfig) -> None:
        self._config = app_config

    @property
    def app_config(self) -> AppConfig:
        return self._config

    async def instruments(self) -> tuple[InstrumentConfig, ...]:
        return self._config.instruments
