/* Single SSE subscription fanned out to every page (bounded ring buffers —
 * observations are droppable by design, exactly like the core bus). */
import { reactive } from "vue";
import type {
  DataPointerEvent,
  DeviceHealthEvent,
  ErrorEvent as BusErrorEvent,
  LogEvent,
  PreviewPayload,
  ProgressEvent,
  RunStateEvent,
} from "../api/contracts";
import type { PhoebeClient } from "../api/client";
import { EventStream } from "../api/sse";

const TOPICS = ["progress", "run_state", "data_pointer", "device_health", "error", "log"];
const LOG_CAP = 500;

export interface LogLine {
  n: number;
  t: string;
  level: "debug" | "info" | "warning" | "error";
  message: string;
}

export const events = reactive({
  connected: false,
  seen: 0,
  logs: [] as LogLine[],
  deviceHealth: {} as Record<string, DeviceHealthEvent>,
  runStates: {} as Record<string, RunStateEvent>,
  progress: {} as Record<string, ProgressEvent>,
  lastPreview: null as { preview: PreviewPayload; taskId: string | null } | null,
});

type Listener = (type: string, ev: Record<string, unknown>) => void;
const listeners = new Set<Listener>();
let logCounter = 0;
let stream: EventStream | null = null;

/** Subscribe a page to raw bus events; returns the unsubscribe. */
export function onBusEvent(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function pushLog(level: LogLine["level"], message: string, t: string): void {
  events.logs.push({ n: ++logCounter, t, level, message });
  if (events.logs.length > LOG_CAP) events.logs.splice(0, events.logs.length - LOG_CAP);
}

function handle(type: string, raw: Record<string, unknown>): void {
  events.seen += 1;
  switch (type) {
    case "run_state": {
      const ev = raw as unknown as RunStateEvent;
      if (ev.task_id) events.runStates[ev.task_id] = ev;
      break;
    }
    case "progress": {
      const ev = raw as unknown as ProgressEvent;
      if (ev.task_id) events.progress[ev.task_id] = ev;
      break;
    }
    case "data_pointer": {
      const ev = raw as unknown as DataPointerEvent;
      if (ev.preview) {
        events.lastPreview = { preview: ev.preview, taskId: ev.task_id ?? null };
      }
      break;
    }
    case "device_health": {
      const ev = raw as unknown as DeviceHealthEvent;
      events.deviceHealth[ev.instrument_id] = ev;
      break;
    }
    case "log": {
      const ev = raw as unknown as LogEvent;
      pushLog(ev.level ?? "info", ev.message, ev.t_wall);
      break;
    }
    case "error": {
      const ev = raw as unknown as BusErrorEvent;
      pushLog("error", `[${ev.code ?? "internal"}] ${ev.message}`, ev.t_wall);
      break;
    }
  }
  for (const fn of listeners) fn(type, raw);
}

export function startEventStream(client: PhoebeClient): void {
  stopEventStream();
  stream = new EventStream(client, TOPICS, {
    onEvent: handle,
    onStatus: (up) => (events.connected = up),
  });
  stream.start();
}

export function stopEventStream(): void {
  stream?.stop();
  stream = null;
  events.connected = false;
}

export function clearLogs(): void {
  events.logs.length = 0;
}
