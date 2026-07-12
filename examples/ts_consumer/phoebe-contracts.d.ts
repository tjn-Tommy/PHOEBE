// Generated from schemas/phoebe-contracts.schema.json — DO NOT EDIT.
// Regenerate with: python tools/gen_ts_types.py
// bundle_format 1, contracts_version 2

/** Stable dispatch/ack vocabulary (plan §6.4).  Values are part of the */
export type AckCode = "accepted" | "queued" | "replayed" | "unknown_command" | "invalid_payload" | "command_id_conflict" | "maintenance_mode" | "plugin_api_incompatible" | "missing_role" | "kind_mismatch" | "health_stale" | "device_not_ready" | "calibration_expired" | "device_busy" | "unknown_task" | "invalid_state" | "internal_error";

/** Outcome of one admission-chain traversal (plan §6.4): a stable code */
export interface AdmissionDecision {
  code: AckCode;
  detail?: string | null;
  error?: ErrorInfo | null;
  task_id?: string | null;
}

export interface CommandAck {
  accepted: boolean;
  code: AckCode;
  command_id: string;
  error?: ErrorInfo | null;
  queued?: boolean;
  reason?: string | null;
  task_id?: string | null;
}

export interface CommandEnvelope {
  command: string;
  command_id: string;
  issued_by?: string;
  payload?: Record<string, unknown>;
  t_wall?: string;
}

/** Operational state for device panels / the future API (plan §3.1 A2). */
export interface ControllerStats {
  instrument_id: string;
  ops_failed?: number;
  ops_ok?: number;
  recent_errors?: string[];
  started_at: string;
}

export interface DataPointer {
  dataset: string;
  index: number;
  run_id: string;
}

export interface DataPointerEvent {
  dataset: string;
  event_type?: "data_pointer";
  index: number;
  preview?: SpectrumPreview | WaveformPreview | ImageThumbnail | ScalarSeries | null;
  run_id: string;
  schema_version?: number;
  seq?: number;
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
}

export interface DeviceHealth {
  detail?: string | null;
  metrics?: Record<string, number>;
  status: "ok" | "degraded" | "error" | "offline";
}

export interface DeviceHealthEvent {
  detail?: string | null;
  event_type?: "device_health";
  instrument_id: string;
  metrics?: Record<string, number>;
  schema_version?: number;
  seq?: number;
  status: "ok" | "degraded" | "error" | "offline";
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
}

export interface DeviceIdentity {
  firmware?: string;
  model: string;
  raw?: string;
  serial?: string;
  vendor: string;
}

/** One device-table row for panels/clients: static config + live */
export interface DeviceStatusView {
  backend: string;
  detail?: string | null;
  instrument_id: string;
  kind: string;
  lifecycle: string;
  model: string;
  role: string;
  stats?: ControllerStats | null;
  vendor: string;
}

/** Stable machine-readable error classes for the wire (plan §6.4). */
export type ErrorCode = "connection" | "timeout" | "protocol" | "device_reported" | "invalid_state" | "safety" | "unsupported_capability" | "contract" | "unsupported_model" | "lease_unavailable" | "device_not_ready" | "cancelled" | "bus_overflow" | "writer_failed" | "config" | "internal";

export interface ErrorEvent {
  code?: ErrorCode;
  error_type: string;
  event_type?: "error";
  instrument_id?: string | null;
  message: string;
  schema_version?: number;
  seq?: number;
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
}

/** Structured error attached to acks and events (plan §6.4): clients read */
export interface ErrorInfo {
  code: ErrorCode;
  error_type?: string;
  instrument_id?: string | null;
  message: string;
}

/** Bus health counters (plan §6.5): published to diagnostics consumers */
export interface EventBusStats {
  current_seq: number;
  failed_subscriptions: number;
  oversize_dropped: number;
  subscriber_count: number;
  total_dropped: number;
}

/** Tiny raster thumbnail (SLM mask, camera frame); PNG, base64-encoded. */
export interface ImageThumbnail {
  height: number;
  png_base64: string;
  preview_type?: "image";
  width: number;
}

export interface InstrumentDescriptor {
  instrument_id: string;
  kind: string;
  model: string;
  provides: string[];
  vendor: string;
}

/** Settings snapshot for pre/post-run baselines (refactor.md §10.5). */
export interface InstrumentSnapshot {
  instrument_id: string;
  taken_at?: string;
  values?: Record<string, string | number | boolean | null>;
}

/** Lifecycle facts, in causal order (plan §6.2).  ``RECOVERED`` is */
export type JournalRecordType = "admitted" | "run_dir_created" | "baseline_captured" | "staged" | "execution_started" | "execution_outcome" | "cleanup_started" | "writer_closed" | "leases_released" | "finalized" | "recovered";

/** Short log excerpt for live UI consoles (full log goes to experiment.jsonl). */
export interface LogEvent {
  event_type?: "log";
  level?: "debug" | "info" | "warning" | "error";
  message: string;
  schema_version?: number;
  seq?: number;
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
}

export interface ProgressEvent {
  event_type?: "progress";
  metrics?: Record<string, number>;
  schema_version?: number;
  seq?: number;
  step: number;
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
  total?: number | null;
}

/** One incomplete run explained by the startup scan (plan §6.2): what the */
export interface RecoveryReport {
  explanation: string;
  last_record: JournalRecordType;
  resolution: "interrupted" | "operator_review_required";
  run_dir: string;
  run_id: string;
  task_id: string;
}

export interface RunJournalRecord {
  detail?: string | null;
  finalized?: "ok" | "degraded" | null;
  outcome?: "completed" | "failed" | "aborted" | null;
  record: JournalRecordType;
  resolution?: "interrupted" | "operator_review_required" | null;
  run_id: string;
  t_mono_ns: number;
  t_wall: string;
  task_id: string;
}

export interface RunManifest {
  app_config_hash?: string;
  code_version?: string;
  command: string;
  config_hash: string;
  config_json: string;
  created_at: string;
  git_commit?: string;
  git_dirty?: boolean;
  instruments?: Record<string, Record<string, unknown>>;
  plugin_id: string;
  run_id: string;
  task_id: string;
}

/** Catalog row: one line per run, queryable without opening run dirs. */
export interface RunResult {
  command: string;
  created_at: string;
  execution_outcome?: "completed" | "failed" | "aborted" | null;
  finalized?: "ok" | "degraded" | null;
  plugin_id: string;
  run_dir: string;
  run_id: string;
  state: string;
  task_id: string;
}

export type RunState = "queued" | "preparing" | "running" | "pausing" | "paused" | "stopping" | "finalizing" | "completed" | "failed" | "aborted";

export interface RunStateEvent {
  event_type?: "run_state";
  final?: boolean;
  reason?: string | null;
  schema_version?: number;
  seq?: number;
  state: RunState;
  t_mono_ns: number;
  t_wall: string;
  task_id?: string | null;
}

/** Named scalar-vs-x series (optimizer metric history, power monitor). */
export interface ScalarSeries {
  name: string;
  preview_type?: "scalar_series";
  x: number[];
  y: number[];
}

/** Down-sampled preview small enough for the bus; cap is part of the schema. */
export interface SpectrumPreview {
  preview_type?: "spectrum";
  x_nm: number[];
  y_dbm: number[];
}

/** Time-domain preview (scope/DAQ channels). */
export interface WaveformPreview {
  preview_type?: "waveform";
  t_s: number[];
  y: number[];
  y_unit?: string;
}

export type GatewayEvent = DataPointerEvent | ProgressEvent | RunStateEvent | DeviceHealthEvent | ErrorEvent | LogEvent;

export type PreviewPayload = SpectrumPreview | WaveformPreview | ImageThumbnail | ScalarSeries;
