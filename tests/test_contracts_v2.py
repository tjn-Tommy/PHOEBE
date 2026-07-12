"""Contracts v2 (plan C-1/C-5): every serializable boundary model JSON
round-trips the day it is defined, codes are stable, and the discriminated
unions parse by discriminator — the wire contract is exercised, not dead
code."""
from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from phoebe.contracts import validate_boundary
from phoebe.contracts.commands import (
    AckCode,
    AdmissionDecision,
    CommandAck,
    CommandEnvelope,
    ack_from_decision,
)
from phoebe.contracts.errors import (
    DeviceNotReadyError,
    DeviceReportedError,
    ErrorCode,
    ErrorInfo,
    InstrumentTimeoutError,
    LeaseUnavailableError,
    PhoebeConfigError,
    error_code_of,
    error_info_of,
)
from phoebe.contracts.events import (
    DataPointerEvent,
    DeviceHealthEvent,
    ErrorEvent,
    EventBusStats,
    GatewayEvent,
    ImageThumbnail,
    LogEvent,
    PreviewPayload,
    ProgressEvent,
    RunStateEvent,
    ScalarSeries,
    SpectrumPreview,
    TracePreview,
    WaveformPreview,
)
from phoebe.contracts.instruments import ControllerStats, DeviceStatusView
from phoebe.contracts.run import (
    JournalRecordType,
    RecoveryReport,
    RunJournalRecord,
    RunResult,
    RunState,
)
from phoebe.core.contracts import InstrumentId, RunId, TaskId, timestamps, utc_now


def roundtrip(model):
    """model → JSON → validate_boundary → identical model."""
    restored = validate_boundary(type(model), json.loads(model.model_dump_json()))
    assert restored == model
    return restored


# ------------------------------------------------------------------ events
def test_every_gateway_event_roundtrips():
    events = [
        DataPointerEvent(task_id=TaskId("t1"), run_id=RunId("r1"),
                         dataset="artifacts.h5:/traces/spectrum", index=3,
                         preview=SpectrumPreview(x_nm=[1.0, 2.0], y_dbm=[-3.0, -4.0]),
                         **timestamps()),
        ProgressEvent(task_id=TaskId("t1"), step=5, total=10,
                      metrics={"peak_dbm": -3.2}, **timestamps()),
        RunStateEvent(task_id=TaskId("t1"), state=RunState.COMPLETED,
                      reason="done", final=True, **timestamps()),
        DeviceHealthEvent(instrument_id=InstrumentId("osa.main"), status="ok",
                          detail="connected", metrics={"temp_c": 32.0},
                          **timestamps()),
        ErrorEvent(task_id=TaskId("t1"), error_type="InstrumentTimeoutError",
                   message="no reply", code=ErrorCode.TIMEOUT,
                   instrument_id=InstrumentId("osa.main"), **timestamps()),
        LogEvent(task_id=TaskId("t1"), level="warning", message="careful",
                 **timestamps()),
    ]
    adapter = TypeAdapter(GatewayEvent)
    for event in events:
        roundtrip(event)
        # the closed union parses the serialized form back to the same type
        parsed = adapter.validate_json(event.model_dump_json())
        assert type(parsed) is type(event)
        assert parsed == event


def test_preview_union_parses_by_discriminator():
    adapter = TypeAdapter(PreviewPayload)
    previews = [
        SpectrumPreview(x_nm=[778.0], y_dbm=[-10.0]),
        WaveformPreview(t_s=[0.0, 1e-9], y=[0.1, 0.2], y_unit="V"),
        ImageThumbnail(width=8, height=8, png_base64="aGk="),
        ScalarSeries(name="peak_dbm", x=[0.0, 1.0], y=[-9.0, -8.5]),
    ]
    for preview in previews:
        parsed = adapter.validate_json(preview.model_dump_json())
        assert type(parsed) is type(preview)
        assert parsed == preview


def test_trace_preview_alias_is_spectrum_preview():
    assert TracePreview is SpectrumPreview
    with pytest.raises(ValidationError):        # cap still part of the schema
        TracePreview(x_nm=[0.0] * 300, y_dbm=[0.0] * 300)


def test_final_flag_defaults_false():
    ev = RunStateEvent(state=RunState.COMPLETED, **timestamps())
    assert ev.final is False


# ---------------------------------------------------------------- commands
def test_command_ack_roundtrips_with_code_and_error():
    ack = CommandAck(
        command_id="c1", accepted=False, code=AckCode.DEVICE_NOT_READY,
        reason="instrument 'osa.main' is not ready (lifecycle state: backoff)",
        error=ErrorInfo(code=ErrorCode.DEVICE_NOT_READY, message="not ready",
                        error_type="DeviceNotReadyError",
                        instrument_id=InstrumentId("osa.main")),
    )
    restored = roundtrip(ack)
    assert restored.code is AckCode.DEVICE_NOT_READY
    assert restored.error is not None
    assert restored.error.code is ErrorCode.DEVICE_NOT_READY


def test_admission_decision_roundtrip_and_projection():
    decision = AdmissionDecision(code=AckCode.QUEUED, detail="waiting",
                                 task_id=TaskId("task_1"))
    roundtrip(decision)
    assert decision.admitted
    ack = ack_from_decision("c9", decision)
    assert ack.accepted and ack.queued and ack.code is AckCode.QUEUED

    rejected = AdmissionDecision(code=AckCode.MAINTENANCE_MODE, detail="down")
    assert not rejected.admitted
    ack = ack_from_decision("c10", rejected)
    assert not ack.accepted and ack.code is AckCode.MAINTENANCE_MODE


def test_command_envelope_roundtrips():
    roundtrip(CommandEnvelope(command_id="c1", command="start_tpa_run",
                              payload={"max_steps": 3}))


def test_ack_code_values_are_stable():
    """Wire values are contract — renaming one breaks every client."""
    assert AckCode.ACCEPTED.value == "accepted"
    assert AckCode.QUEUED.value == "queued"
    assert AckCode.REPLAYED.value == "replayed"
    assert AckCode.COMMAND_ID_CONFLICT.value == "command_id_conflict"
    assert AckCode.MAINTENANCE_MODE.value == "maintenance_mode"
    assert AckCode.PLUGIN_API_INCOMPATIBLE.value == "plugin_api_incompatible"
    assert AckCode.MISSING_ROLE.value == "missing_role"
    assert AckCode.KIND_MISMATCH.value == "kind_mismatch"
    assert AckCode.HEALTH_STALE.value == "health_stale"
    assert AckCode.DEVICE_NOT_READY.value == "device_not_ready"
    assert AckCode.DEVICE_BUSY.value == "device_busy"
    assert AckCode.UNKNOWN_COMMAND.value == "unknown_command"
    assert AckCode.INVALID_PAYLOAD.value == "invalid_payload"
    assert AckCode.UNKNOWN_TASK.value == "unknown_task"
    assert AckCode.INVALID_STATE.value == "invalid_state"
    assert AckCode.INTERNAL_ERROR.value == "internal_error"


# ------------------------------------------------------------------ errors
def test_error_code_of_maps_by_type_not_text():
    assert error_code_of(InstrumentTimeoutError("whatever text")) is ErrorCode.TIMEOUT
    assert error_code_of(DeviceReportedError("x")) is ErrorCode.DEVICE_REPORTED
    assert error_code_of(LeaseUnavailableError("osa.main")) is ErrorCode.LEASE_UNAVAILABLE
    assert error_code_of(DeviceNotReadyError("osa.main", "backoff")) is ErrorCode.DEVICE_NOT_READY
    assert error_code_of(PhoebeConfigError("bad")) is ErrorCode.CONFIG
    assert error_code_of(TimeoutError()) is ErrorCode.TIMEOUT
    assert error_code_of(RuntimeError("mystery")) is ErrorCode.INTERNAL


def test_error_info_of_carries_instrument_attribution():
    info = error_info_of(InstrumentTimeoutError("no reply",
                                                instrument_id="osa.main"))
    assert info.code is ErrorCode.TIMEOUT
    assert info.instrument_id == "osa.main"
    assert info.error_type == "InstrumentTimeoutError"
    roundtrip(info)


# ------------------------------------------------------------ run contracts
def test_run_journal_record_roundtrips():
    rec = RunJournalRecord(record=JournalRecordType.EXECUTION_OUTCOME,
                           task_id=TaskId("task_1"), run_id=RunId("run_1"),
                           outcome="failed", detail="boom", **timestamps())
    roundtrip(rec)
    final = RunJournalRecord(record=JournalRecordType.FINALIZED,
                             task_id=TaskId("task_1"), run_id=RunId("run_1"),
                             finalized="degraded", **timestamps())
    roundtrip(final)


def test_run_result_and_recovery_report_roundtrip():
    roundtrip(RunResult(run_id=RunId("r1"), task_id=TaskId("t1"),
                        plugin_id="org.lab.tpa_multiplier",
                        command="start_tpa_run", created_at=utc_now(),
                        run_dir="2026_x", state="completed",
                        execution_outcome="completed", finalized="ok"))
    roundtrip(RecoveryReport(run_id=RunId("r1"), task_id=TaskId("t1"),
                             run_dir="2026_x",
                             last_record=JournalRecordType.EXECUTION_STARTED,
                             resolution="operator_review_required",
                             explanation="process died while executing"))


def test_device_views_roundtrip():
    stats = ControllerStats(instrument_id=InstrumentId("osa.main"),
                            started_at=utc_now(), ops_ok=5, ops_failed=1,
                            recent_errors=("2026 timeout",))
    roundtrip(stats)
    roundtrip(DeviceStatusView(instrument_id=InstrumentId("osa.main"),
                               kind="spectrum_analyzer", vendor="yokogawa",
                               model="aq6370", role="main_osa", backend="sim",
                               lifecycle="ready", stats=stats))
    roundtrip(EventBusStats(current_seq=10, total_dropped=0,
                            oversize_dropped=0, failed_subscriptions=0,
                            subscriber_count=2))
