"""PyQt5 UI shell (refactor.md §13.2; evolution plan PR C-4).

Panels do exactly three things: assemble forms into strongly-typed command
payloads submitted through the RunService, refresh themselves from EventBus
events delivered by the UiEventBridge, and query the service layer for
snapshots (runs catalog, device stats, bus health).  No Driver/Controller
imports, no core reach-ins — the import-linter contracts hold.

Acks are parsed by ``AckCode`` — zero prose (plan §6.4).
"""
from __future__ import annotations

import uuid
from typing import Any

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..contracts.commands import CommandAck, CommandEnvelope
from ..contracts.run import RunState
from ..core.config import AppConfig
from ..services import ServiceHub
from .bridge import UiEventBridge
from .forms import SchemaForm

_ACTIVE_STATES = {RunState.QUEUED, RunState.PREPARING, RunState.RUNNING,
                  RunState.PAUSING, RunState.PAUSED, RunState.STOPPING,
                  RunState.FINALIZING}


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


class RunControlPanel(QGroupBox):
    """Form → CommandEnvelope; run status + pause/resume/cancel built-ins.

    Forms are **generated from each plugin's config schema** (PR D-2) — the
    contract is the single source of defaults/ranges, so the panel can never
    drift from the plugin again (H12)."""

    submit_requested = pyqtSignal(str, dict)     # (command, payload)
    builtin_requested = pyqtSignal(str)          # "pause" | "resume" | "cancel"

    def __init__(self, services: ServiceHub) -> None:
        super().__init__("Run control")
        self.tabs = QTabWidget()
        commands = services.call(services.plugins.commands()).result(timeout=10)
        for command in commands:
            schema = services.call(
                services.plugins.config_schema(command)).result(timeout=10)
            if schema is not None:
                self.tabs.addTab(SchemaForm(command, schema), command)

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
        TaskManager re-broadcasts the terminal state with ``final=True``."""
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
    """Live previews (rendered by PreviewPayload discriminator) + metric history."""

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

    def show_preview(self, preview: Any) -> None:
        """Render by discriminator (plan §6.5); unknown kinds are ignored."""
        kind = getattr(preview, "preview_type", "")
        if kind == "spectrum":
            self._spectrum_curve.setData(preview.x_nm, preview.y_dbm)
        elif kind == "waveform":
            self._spectrum_curve.setData(preview.t_s, preview.y)
        elif kind == "scalar_series":
            self._metric_curve.setData(preview.x, preview.y)

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


class RunsPanel(QGroupBox):
    """Run catalog view (journal projection via RunService)."""

    _COLUMNS = ("run", "plugin", "state", "outcome", "finalized")
    _rows_ready = pyqtSignal(object)             # list[RunResult], from the loop

    def __init__(self, services: ServiceHub) -> None:
        super().__init__("Runs")
        self._services = services
        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.refresh_btn = QPushButton("Refresh")
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.refresh_btn)
        self.refresh_btn.clicked.connect(self.refresh)
        self._rows_ready.connect(self._fill)

    def refresh(self) -> None:
        future = self._services.call(self._services.runs.list_runs(limit=50))
        future.add_done_callback(
            lambda fut: None if fut.cancelled() or fut.exception()
            else self._rows_ready.emit(fut.result()))

    def _fill(self, results: Any) -> None:
        self.table.setRowCount(len(results))
        for row, res in enumerate(results):
            cells = (str(res.run_id), res.plugin_id, res.state,
                     res.execution_outcome or "", res.finalized or "")
            for col, text in enumerate(cells):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()


class DiagnosticsPanel(QGroupBox):
    """Device operational stats + bus health (plan §6.5: drop counters are
    published, not process-private)."""

    _COLUMNS = ("instrument", "lifecycle", "ops ok", "ops failed", "last error")
    _snapshot_ready = pyqtSignal(object, object)  # (device rows, bus stats)

    def __init__(self, services: ServiceHub) -> None:
        super().__init__("Diagnostics")
        self._services = services
        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.bus_label = QLabel("bus: —")
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.bus_label)
        self._snapshot_ready.connect(self._fill)
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        if not self.isVisible():
            return

        async def snapshot():
            rows = await self._services.devices.table()
            stats = await self._services.events.bus_stats()
            return rows, stats

        future = self._services.call(snapshot())
        future.add_done_callback(
            lambda fut: None if fut.cancelled() or fut.exception()
            else self._snapshot_ready.emit(*fut.result()))

    def _fill(self, rows: Any, bus_stats: Any) -> None:
        self.table.setRowCount(len(rows))
        for row, view in enumerate(rows):
            last_error = (view.stats.recent_errors[-1]
                          if view.stats and view.stats.recent_errors else "")
            cells = (str(view.instrument_id), view.lifecycle,
                     str(view.stats.ops_ok if view.stats else 0),
                     str(view.stats.ops_failed if view.stats else 0),
                     last_error)
            for col, text in enumerate(cells):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.bus_label.setText(
            f"bus: seq {bus_stats.current_seq} · dropped {bus_stats.total_dropped}"
            f" · oversize {bus_stats.oversize_dropped}"
            f" · failed subs {bus_stats.failed_subscriptions}")


class MainWindow(QMainWindow):
    """Wires panels to the service layer (commands in) and the bridge
    (events out)."""

    _ack_received = pyqtSignal(object)           # CommandAck from the loop thread

    def __init__(self, config: AppConfig, services: ServiceHub,
                 bridge: UiEventBridge) -> None:
        super().__init__()
        self.setWindowTitle("PHOEBE — experiment control")
        self.resize(1280, 800)
        self._services = services
        self._task_id: str | None = None

        self.devices = DevicePanel(config)
        self.run_control = RunControlPanel(services)
        self.plots = PlotPanel()
        self.log = LogPanel()
        self.runs = RunsPanel(services)
        self.diagnostics = DiagnosticsPanel(services)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.devices, 1)
        left_layout.addWidget(self.run_control, 0)
        left_layout.addWidget(self.log, 1)
        right = QTabWidget()
        right.addTab(self.plots, "Live")
        right.addTab(self.runs, "Runs")
        right.addTab(self.diagnostics, "Diagnostics")
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        self.run_control.submit_requested.connect(self._submit_command)
        self.run_control.builtin_requested.connect(self._submit_builtin)
        self._ack_received.connect(self._on_ack)
        bridge.event_received.connect(self._on_event)
        self.runs.refresh()

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
        future = self._services.call(self._services.runs.submit(envelope))
        # the callback fires on the loop thread; the signal hops back to Qt
        future.add_done_callback(
            lambda fut: self._ack_received.emit(
                fut.exception() or fut.result()))

    def _on_ack(self, ack: CommandAck | BaseException) -> None:
        # No modal dialogs here: acks arrive asynchronously and rejections
        # (e.g. device_busy) are routine — status bar + log are enough.
        if isinstance(ack, BaseException):
            self.log.append(f"[gateway] ERROR: {ack}")
            self.statusBar().showMessage(f"gateway error: {ack}", 5000)
            return
        if not ack.accepted:
            detail = f" — {ack.reason}" if ack.reason else ""
            self.log.append(f"[gateway] rejected ({ack.code.value}){detail}")
            self.statusBar().showMessage(f"rejected: {ack.code.value}", 5000)
            return
        if ack.task_id is not None:
            self._task_id = str(ack.task_id)
            self.run_control.set_task(self._task_id)
        self.log.append(f"[gateway] {ack.code.value}"
                        + (f" → {ack.task_id}" if ack.task_id else ""))

    # ------------------------------------------------------ events (from loop)
    def _on_event(self, event: Any) -> None:
        kind = getattr(event, "event_type", "")
        if kind == "device_health":
            self.devices.update_health(str(event.instrument_id), event.status,
                                       event.detail or "")
        elif kind == "run_state":
            if self._task_id is None or str(event.task_id) == self._task_id:
                final = not event.state.is_terminal or event.final
                self.run_control.set_run_state(event.state, final=final)
                self.log.append(f"[{event.task_id}] state → {event.state.value}"
                                + (f" ({event.reason})" if event.reason else ""))
            if event.state.is_terminal and event.final:
                self.runs.refresh()              # catalog row just finalized
        elif kind == "progress":
            self.run_control.set_progress(event.step, event.total)
            peak = event.metrics.get("peak_dbm")
            if peak is not None:
                self.plots.append_metric(event.step, peak)
        elif kind == "data_pointer":
            if event.preview is not None:
                self.plots.show_preview(event.preview)
        elif kind == "error":
            self.log.append(f"[error:{event.code.value}] {event.error_type}: "
                            f"{event.message}")
        elif kind == "log":
            self.log.append(f"[{event.level}] {event.message}")
