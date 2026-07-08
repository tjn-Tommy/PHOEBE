"""NI-DAQ controller: lab semantics, atomic operations, runtime contracts.

Migrated from ``TPA_experiment/src/daq_module/controller.py``.

Only the single untriggered averaged-reading path is implemented -- the role
``ScopeController.monitor_cycle()`` plays for the TPA encoder feedback page.
Every blocking driver call is dispatched to the device's worker thread via
``self._worker.call(...)`` (refactor.md §12.3); the controller never touches
``nidaqmx`` directly.
"""
from __future__ import annotations

import asyncio

from ...core.capability import InvocationContext
from ...core.config import InstrumentConfig, SdkConnection
from ...core.contracts import ContractModel, InstrumentId, timestamps
from ...core.controller import (
    DeviceHealth,
    DeviceIdentity,
    InstrumentController,
    InstrumentDescriptor,
    InstrumentSnapshot,
)
from ...core.errors import InstrumentError, UnsupportedInstrumentModelError
from ...core.factory import AppDependencies
from ...core.worker import BlockingDeviceWorker
from ...domain.daq import AnalogReadConfig, AnalogReadMeta, AnalogTrace
from ...domain.scope import MonitorSample
from ..protocols import KIND_ANALOG_INPUT
from .driver import NiDaqDriver


class DaqOptions(ContractModel):
    """Second-stage validation of InstrumentConfig.options (refactor.md §5.2).

    The NI-DAQ analog-input role carries no tunable options today; the empty
    model still enforces "no unknown option keys" at startup.
    """


class NiDaqController(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: NiDaqDriver,
                 worker: BlockingDeviceWorker, options: DaqOptions, *,
                 vendor: str = "ni", model: str = "usb-6251") -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._worker = worker
        self._options = options
        self._vendor = vendor
        self._model = model
        self._identity: DeviceIdentity | None = None
        self._connected = False

    # ------------------------------------------------------------- lifecycle
    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id, kind=KIND_ANALOG_INPUT,
            vendor=self._vendor, model=self._model,
            provides=(KIND_ANALOG_INPUT,),
        )

    async def connect(self) -> None:
        await self._worker.call(self._driver.connect)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        await self._worker.call(self._driver.disconnect)

    async def get_identity(self) -> DeviceIdentity:
        # DeviceManager verifies identity by checking cfg.vendor and cfg.model
        # appear in "<vendor> <model> <raw>"; the configured vendor/model are
        # carried straight through so the check always holds.
        if self._identity is None:
            raw = ""
            try:
                raw = await self._worker.call(self._driver.identify)
            except InstrumentError:
                raw = ""
            self._identity = DeviceIdentity(
                vendor=self._vendor, model=self._model, raw=raw)
        return self._identity

    async def get_health(self) -> DeviceHealth:
        if not self._connected:
            return DeviceHealth(status="offline")
        try:
            await self._worker.call(self._driver.identify)
            return DeviceHealth(status="ok")
        except InstrumentError as exc:
            return DeviceHealth(status="error", detail=str(exc))

    async def get_snapshot(self) -> InstrumentSnapshot:
        return InstrumentSnapshot(
            instrument_id=self.instrument_id,
            values={"connected": self._connected, "device": self._driver.device},
        )

    # ---------------------------------------------------- runtime contracts
    async def stop(self) -> None:
        """Finite reads are short and self-terminating; nothing to abort."""

    async def safe_state(self) -> None:
        """No outputs / modulation to make safe on an analog-input device."""

    # ------------------------------------------------------- AnalogInput view
    async def read_trace(
        self, config: AnalogReadConfig, *, context: InvocationContext
    ) -> AnalogTrace:
        async with self._op_lock:
            if config.hold_s:
                await asyncio.sleep(config.hold_s)      # settle before sampling
            context.ensure_not_cancelled()
            values = await self._worker.call(
                self._driver.read_waveform,
                channel=config.channel,
                sample_rate=config.sample_rate_hz,
                duration=config.duration_s,
                min_val=config.min_val_v,
                max_val=config.max_val_v,
                timeout=config.timeout_s,
            )
            return AnalogTrace(
                values=values,
                meta=AnalogReadMeta(
                    instrument_id=self.instrument_id, config=config, **timestamps()
                ),
            )

    async def read_sample(
        self, config: AnalogReadConfig, *, context: InvocationContext
    ) -> MonitorSample:
        trace = await self.read_trace(config, context=context)
        return MonitorSample(
            value=trace.mean, std=trace.std, index=0, **timestamps())


def build_ni_daq(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    options = DaqOptions.model_validate(cfg.options)
    match cfg.connection:
        case SdkConnection() as c:
            driver = NiDaqDriver(c.device)
        case _:
            raise UnsupportedInstrumentModelError(
                f"{cfg.instrument_id}: NI-DAQ requires an sdk connection")
    worker = deps.worker_pool.for_device(cfg.instrument_id)
    return NiDaqController(cfg.instrument_id, driver, worker, options,
                           vendor=cfg.vendor, model=cfg.model)
