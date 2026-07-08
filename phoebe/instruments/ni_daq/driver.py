"""NI-DAQmx analog-input driver (synchronous; runs on the device worker thread).

Migrated from ``TPA_experiment/src/daq_module/driver.py`` (``NIDAQDriver`` over
the ``nidaqmx`` package, e.g. a USB-6251).

These methods are blocking on purpose: the controller wraps every call in
``worker.call(...)`` so they execute on the device's dedicated worker thread
(refactor.md §12.3).  ``nidaqmx`` is imported lazily inside each method so the
package imports fine when the vendor package is not installed.
"""
from __future__ import annotations

import numpy as np

from ...core.errors import InstrumentConnectionError, InstrumentError


class NiDaqDriver:
    """Single-device NI-DAQmx analog-input driver.

    ``connect()`` verifies the device is reachable; ``read_waveform()`` performs
    one untriggered finite acquisition -- the PC arms the task, blocks for
    ``duration`` seconds, then stops it -- and returns the raw voltage samples.
    """

    def __init__(self, device: str) -> None:
        self._device = str(device)
        self._connected = False

    @property
    def device(self) -> str:
        return self._device

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        try:
            from nidaqmx.system import System
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise InstrumentConnectionError(
                "nidaqmx is required for the NI-DAQ driver; "
                "install with `pip install nidaqmx`"
            ) from exc
        try:
            devices = [d.name for d in System.local().devices]
        except Exception as exc:
            raise InstrumentConnectionError(
                f"failed to query NI-DAQmx system: {exc}") from exc
        if self._device not in devices:
            raise InstrumentConnectionError(
                f"{self._device!r} not found "
                f"(available: {', '.join(devices) or 'none'})"
            )
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise InstrumentConnectionError(
                f"NI-DAQ {self._device} is not connected; call connect() first")

    def identify(self) -> str:
        self._ensure_connected()
        from nidaqmx.system import System

        dev = System.local().devices[self._device]
        return f"{self._device} ({dev.product_type})"

    def read_waveform(
        self,
        *,
        channel: str,
        sample_rate: float,
        duration: float,
        min_val: float,
        max_val: float,
        timeout: float,
    ) -> np.ndarray:
        """One untriggered finite acquisition; returns the raw voltage samples."""
        self._ensure_connected()
        import nidaqmx
        from nidaqmx.constants import AcquisitionType

        n_samples = max(1, int(round(sample_rate * duration)))
        try:
            with nidaqmx.Task() as task:
                task.ai_channels.add_ai_voltage_chan(
                    f"{self._device}/{channel}", min_val=min_val, max_val=max_val
                )
                task.timing.cfg_samp_clk_timing(
                    sample_rate,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=n_samples,
                )
                task.start()
                values = task.read(
                    number_of_samples_per_channel=n_samples, timeout=timeout)
                task.stop()
        except InstrumentError:
            raise
        except Exception as exc:
            raise InstrumentError(
                f"NI-DAQ read failed on {self._device}/{channel}: {exc}") from exc

        return np.asarray(values, dtype=float)
