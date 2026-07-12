// Sample consumer of the generated PHOEBE contract types (plan §6.7).
// A future Vue/Tauri frontend consumes the same declarations; this file just
// proves the generated .d.ts is usable TypeScript.
//
//   tsc --noEmit --strict sample.ts     (requires a TypeScript toolchain)

import type {
  AckCode,
  CommandAck,
  CommandEnvelope,
  GatewayEvent,
  RunResult,
} from "./phoebe-contracts";

const envelope: CommandEnvelope = {
  command_id: "ui-0001",
  command: "start_tpa_run",
  payload: { max_steps: 8, seed: 42 },
};

function isAccepted(code: AckCode): boolean {
  return code === "accepted" || code === "queued" || code === "replayed";
}

export function onAck(ack: CommandAck): string {
  // zero prose: clients branch on the code, never parse `reason`
  if (!isAccepted(ack.code)) {
    return `rejected (${ack.code})`;
  }
  return ack.task_id ?? "accepted";
}

export function describeEvent(event: GatewayEvent): string {
  switch (event.event_type) {
    case "run_state":
      return `run ${event.task_id}: ${event.state}${event.final ? " (final)" : ""}`;
    case "device_health":
      return `${event.instrument_id}: ${event.status}`;
    case "data_pointer":
      return `data → ${event.dataset}[${event.index}]`;
    case "progress":
      return `step ${event.step}/${event.total ?? "?"}`;
    case "error":
      return `error ${event.code}: ${event.message}`;
    case "log":
      return `[${event.level}] ${event.message}`;
  }
}

export function summarize(runs: RunResult[]): string[] {
  return runs.map((r) => `${r.run_id}: ${r.state} (${r.finalized ?? "…"})`);
}

export { envelope };
