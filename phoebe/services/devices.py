"""Device service: inventory table, lifecycle actions, operational stats."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..contracts.instruments import ControllerStats, DeviceStatusView

if TYPE_CHECKING:
    from ..contracts.base import InstrumentId
    from ..core.config import AppConfig
    from ..core.device_manager import DeviceManager


class DeviceService:
    def __init__(self, *, device_manager: DeviceManager, app_config: AppConfig) -> None:
        self._dm = device_manager
        self._config = app_config

    async def table(self) -> list[DeviceStatusView]:
        """One row per configured instrument: static config + live lifecycle
        + stats — everything the device panel renders."""
        rows: list[DeviceStatusView] = []
        stats = self._dm.stats_all()
        for cfg in self._config.instruments:
            supervisor = self._dm.supervisor(cfg.instrument_id)
            rows.append(DeviceStatusView(
                instrument_id=cfg.instrument_id, kind=cfg.kind,
                vendor=cfg.vendor, model=cfg.model, role=cfg.role,
                backend=cfg.backend,
                lifecycle=self._dm.lifecycle_state(cfg.instrument_id).value,
                detail=supervisor.detail if supervisor is not None else None,
                stats=stats.get(str(cfg.instrument_id)),
            ))
        return rows

    async def stats(self) -> dict[str, ControllerStats]:
        return self._dm.stats_all()

    async def reconnect(self, instrument_id: InstrumentId) -> bool:
        return await self._dm.reconnect_instrument(instrument_id)

    async def disable(self, instrument_id: InstrumentId) -> None:
        await self._dm.disable_instrument(instrument_id)

    async def health_check_all(self) -> None:
        await self._dm.health_check_all()
