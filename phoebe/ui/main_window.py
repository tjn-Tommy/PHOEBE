"""PyQt5 UI shell (refactor.md §13.2).

Panels do exactly two things: assemble forms into strongly-typed command
payloads sent through the Gateway, and refresh themselves from EventBus
events delivered by the UiEventBridge.  No Driver/Controller imports, no
direct instrument calls — the import-linter contract (§18 rule 9) holds.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pyqtgraph as pg
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.config import AppConfig
from ..core.events import RunState
from ..core.gateway import CommandAck, CommandEnvelope, Gateway
from .bridge import UiEventBridge

_ACTIVE_STATES = {RunState.QUEUED, RunState.RUNNING, RunState.PAUSING,
                  RunState.PAUSED, RunState.STOPPING}


class DevicePanel(QGroupBox):
    """Inventory table refreshed by DeviceHealthEvents."""

    _COLUMNS = ("instrument", "kind", "role", "backend", "status", "detail")

    def __init__(self, config: AppConfig) -> None:
        super().__init__("Devices")
        self._rows: dict[str, int] = {}
        self.table = QTableWidget(len(config.instruments), len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, inst in enumerate(config.instruments):
            self._rows[str(inst.instrument_id)] = row
            for col, text in enumerate((inst.instrument_id, inst.kind, inst.role,
                                        inst.backend, "…", "")):
                self.table.setItem(row, col, QTableWidgetItem(str(text)))
        self.table.resizeColumnsToContents()
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

    def update_health(self, instrument_id: str, status: str, detail: str) -> None:
        row = self._rows.get(instrument_id)
        if row is None:
            return
        item = QTableWidgetItem(status)
        color = {"ok": Qt.darkGreen, "degraded": Qt.darkYellow,
                 "error": Qt.red, "offline": Qt.gray}.get(status, Qt.black)
        item.setForeground(color)
        self.table.setItem(row, 4, item)
        self.table.setItem(row, 5, QTableWidgetItem(detail or ""))


class _ScanForm(QWidget):
    """Shared OSA scan sub-form (center / span / points)."""

    def __init__(self) -> None:
        super().__init__()
        self.center = QDoubleSpinBox(minimum=1.0, maximum=19_999.0,
                                     value=778.0, decimals=3, suffix=" nm")
        self.span = QDoubleSpinBox(minimum=0.01, maximum=1500.0, value=8.0,
                                   decimals=3, suffix=" nm")
        self.points = QSpinBox(minimum=11, maximum=100_001, value=501)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Center", self.center)
        form.addRow("Span", self.span)
        form.addRow("Points", self.points)

    def payload(self) -> dict[str, Any]:
        return {"center_nm": self.center.value(), "span_nm": self.span.value(),
                "points": self.points.value()}


class TpaForm(QWidget):
    command = "start_tpa_run"

    def __init__(self) -> None:
        super().__init__()
        self.max_steps = QSpinBox(minimum=1, maximum=1_000_000, value=50)
        self.seed = QSpinBox(minimum=0, maximum=2**31 - 1, value=0)
        self.scan = _ScanForm()
        form = QFormLayout(self)
        form.addRow("Max steps", self.max_steps)
        form.addRow("Seed", self.seed)
        form.addRow(self.scan)

    def payload(self) -> dict[str, Any]:
        return {"max_steps": self.max_steps.value(), "seed": self.seed.value(),
                "scan": self.scan.payload()}


class GridForm(QWidget):
    command = "start_grid_scan"

    def __init__(self) -> None:
        super().__init__()
        self.levels = QLineEdit("0, 128, 256, 384, 512")
        self.scan = _ScanForm()
        form = QFormLayout(self)
        form.addRow("SLM levels", self.levels)
        form.addRow(self.scan)

    def payload(self) -> dict[str, Any]:
        levels = [float(part) for part in self.levels.text().split(",")
                  if part.strip()]
        return {"levels": levels, "scan": self.scan.payload()}


class RunControlPanel(QGroupBox):
    """Form → CommandEnvelope; run status + pause/resume/cancel built-ins."""

    submit_requested = pyqtSignal(str, dict)     # (command, payload)
    builtin_requested = pyqtSignal(str)          # "pause" | "resume" | "cancel"

    def __init__(self) -> None:
        super().__init__("Run control")
        self.tabs = QTabWidget()
        self.tpa_form = TpaForm()
        self.grid_form = GridForm()
        self.tabs.addTab(self.tpa_form, "TPA search")
        self.tabs.addTab(self.grid_form, "Grid scan")

        self.start_btn = QPushButton("Start")
        self.pause_btn = QPushButton("Pause")
        self.resume_btn = QPushButton("Resume")
        self.cancel_btn = QPushButton("Cancel")
        buttons = QHBoxLayout()
        for btn in (self.start_btn, self.pause_btn, self.resume_btn,
                    self.cancel_btn):
            buttons.addWidget(btn)

        self.state_label = QLabel("idle")
        self.task_label = QLabel("—")
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        status = QFormLayout()
        status.addRow("Task", self.task_label)
        status.addRow("State", self.state_label)
        status.addRow("Progress", self.progress)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(buttons)
        layout.addLayout(status)

        self.start_btn.clicked.connect(self._on_start)
        self.pause_btn.clicked.connect(lambda: self.builtin_requested.emit("pause"))
        self.resume_btn.clicked.connect(lambda: self.builtin_requested.emit("resume"))
        self.cancel_btn.clicked.connect(lambda: self.builtin_requested.emit("cancel"))
        self.set_run_state(None)

    def _on_start(self) -> None:
        form = self.tabs.currentWidget()
        try:
            payload = form.payload()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid input", str(exc))
            return
        self.submit_requested.emit(form.command, payload)

    def set_task(self, task_id: str | None) -> None:
        self.task_label.setText(task_id or "—")

    def set_run_state(self, state: RunState | None, *, final: bool = True) -> None:
        """``final=False`` marks a terminal state whose cleanup (lease release,
        writer flush) is still running — Start stays disabled until the
        TaskManager re-broadcasts the terminal state with reason="final"."""
        self.state_label.setText(state.value if state else "idle")
        active = (state in _ACTIVE_STATES) if state else False
        self.start_btn.setEnabled(not active and final)
        self.pause_btn.setEnabled(state is RunState.RUNNING)
        self.resume_btn.setEnabled(state is RunState.PAUSED)
        self.cancel_btn.setEnabled(active)

    def set_progress(self, step: int, total: int | None) -> None:
        self.progress.setMaximum(total or 0)     # 0 → busy indicator
        self.progress.setValue(step)


class PlotPanel(QWidget):
    """Live spectrum preview (from DataPointerEvents) + metric history."""

    def __init__(self) -> None:
        super().__init__()
        pg.setConfigOptions(antialias=True)
        self.spectrum = pg.PlotWidget(title="Spectrum preview")
        self.spectrum.setLabel("bottom", "wavelength", units="nm")
        self.spectrum.setLabel("left", "power", units="dBm")
        self._spectrum_curve = self.spectrum.plot(pen=pg.mkPen(width=2))

        self.metric = pg.PlotWidget(title="peak_dbm vs step")
        self.metric.setLabel("bottom", "step")
        self.metric.setLabel("left", "peak", units="dBm")
        self._metric_curve = self.metric.plot(
            pen=None, symbol="o", symbolSize=5)
        self._steps: list[float] = []
        self._peaks: list[float] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self.spectrum)
        layout.addWidget(self.metric)

    def show_preview(self, x_nm: list[float], y_dbm: list[float]) -> None:
        self._spectrum_curve.setData(x_nm, y_dbm)

    def append_metric(self, step: int, peak_dbm: float) -> None:
        self._steps.append(step)
        self._peaks.append(peak_dbm)
        self._metric_curve.setData(self._steps, self._peaks)

    def reset_metrics(self) -> None:
        self._steps.clear()
        self._peaks.clear()
        self._metric_curve.setData([], [])


class LogPanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Events")
        self.text = QPlainTextEdit(readOnly=True, maximumBlockCount=2000)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)

    def append(self, line: str) -> None:
        self.text.appendPlainText(line)


class MainWindow(QMainWindow):
    """Wires panels to the Gateway (commands in) and the bridge (events out)."""

    _ack_received = pyqtSignal(object)           # CommandAck from the loop thread

    def __init__(self, config: AppConfig, gateway: Gateway,
                 loop: asyncio.AbstractEventLoop, bridge: UiEventBridge) -> None:
        super().__init__()
        self.setWindowTitle("PHOEBE — experiment control")
        self.resize(1280, 800)
        self._gateway = gateway
        self._loop = loop
        self._task_id: str | None = None

        self.devices = DevicePanel(config)
        self.run_control = RunControlPanel()
        self.plots = PlotPanel()
        self.log = LogPanel()

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.devices, 1)
        left_layout.addWidget(self.run_control, 0)
        left_layout.addWidget(self.log, 1)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.plots)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        self.run_control.submit_requested.connect(self._submit_command)
        self.run_control.builtin_requested.connect(self._submit_builtin)
        self._ack_received.connect(self._on_ack)
        bridge.event_received.connect(self._on_event)

    # ---------------------------------------------------- commands (into loop)
    def _submit_command(self, command: str, payload: dict) -> None:
        self.plots.reset_metrics()
        self._submit(CommandEnvelope(command_id=f"ui-{uuid.uuid4().hex[:8]}",
                                     command=command, payload=payload))

    def _submit_builtin(self, command: str) -> None:
        if self._task_id is None:
            return
        self._submit(CommandEnvelope(command_id=f"ui-{uuid.uuid4().hex[:8]}",
                                     command=command,
                                     payload={"task_id": self._task_id}))

    def _submit(self, envelope: CommandEnvelope) -> None:
        future = self._gateway.submit_threadsafe(envelope, self._loop)
        # the callback fires on the loop thread; the signal hops back to Qt
        future.add_done_callback(
            lambda fut: self._ack_received.emit(
                fut.exception() or fut.result()))

    def _on_ack(self, ack: CommandAck | BaseException) -> None:
        # No modal dialogs here: acks arrive asynchronously and rejections
        # (e.g. "423 locked") are routine — status bar + log are enough.
        if isinstance(ack, BaseException):
            self.log.append(f"[gateway] ERROR: {ack}")
            self.statusBar().showMessage(f"gateway error: {ack}", 5000)
            return
        if not ack.accepted:
            self.log.append(f"[gateway] rejected: {ack.reason}")
            self.statusBar().showMessage(f"rejected: {ack.reason}", 5000)
            return
        if ack.task_id is not None:
            self._task_id = str(ack.task_id)
            self.run_control.set_task(self._task_id)
        self.log.append(f"[gateway] accepted"
                        + (f" → {ack.task_id}" if ack.task_id else "")
                        + (" (queued)" if ack.queued else ""))

    # ------------------------------------------------------ events (from loop)
    def _on_event(self, event: Any) -> None:
        kind = getattr(event, "event_type", "")
        if kind == "device_health":
            self.devices.update_health(str(event.instrument_id), event.status,
                                       event.detail or "")
        elif kind == "run_state":
            if self._task_id is None or str(event.task_id) == self._task_id:
                final = not event.state.is_terminal or event.reason == "final"
                self.run_control.set_run_state(event.state, final=final)
                self.log.append(f"[{event.task_id}] state → {event.state.value}"
                                + (f" ({event.reason})" if event.reason else ""))
        elif kind == "progress":
            self.run_control.set_progress(event.step, event.total)
            peak = event.metrics.get("peak_dbm")
            if peak is not None:
                self.plots.append_metric(event.step, peak)
        elif kind == "data_pointer":
            if event.preview is not None:
                self.plots.show_preview(event.preview.x_nm, event.preview.y_dbm)
        elif kind == "error":
            self.log.append(f"[error] {event.error_type}: {event.message}")
        elif kind == "log":
            self.log.append(f"[{event.level}] {event.message}")
