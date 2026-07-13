/* Fetch-based SSE consumer for /api/v1/events/stream.
 *
 * Native EventSource cannot set headers, so the session token rides an
 * explicit fetch; frames are `id: seq` + `event:` + `data:` per the E-2
 * contract.  On reconnect, `since_seq` replays the gap from the bus ring;
 * a `stream_reset` event means the ring overflowed — we drop the cursor and
 * resubscribe from the retained snapshot. */
import type { PhoebeClient } from "./client";

export interface StreamHandlers {
  onEvent(type: string, ev: Record<string, unknown>): void;
  onStatus(connected: boolean): void;
}

const RETRY_MS = 3000;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class EventStream {
  lastSeq = 0;
  private stopped = true;
  private ctrl: AbortController | null = null;

  constructor(
    private client: PhoebeClient,
    private topics: string[],
    private handlers: StreamHandlers,
  ) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    void this.loop();
  }

  stop(): void {
    this.stopped = true;
    this.ctrl?.abort();
    this.handlers.onStatus(false);
  }

  private async loop(): Promise<void> {
    while (!this.stopped) {
      try {
        const params = new URLSearchParams({ topics: this.topics.join(",") });
        if (this.lastSeq > 0) params.set("since_seq", String(this.lastSeq));
        this.ctrl = new AbortController();
        const res = await fetch(`${this.client.apiRoot}/events/stream?${params}`, {
          headers: this.client.headers(),
          signal: this.ctrl.signal,
        });
        if (res.status === 401 || res.status === 403) {
          this.handlers.onStatus(false);
          return; // credentials are wrong — reconnecting cannot help
        }
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        this.handlers.onStatus(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) {
            this.handleFrame(buf.slice(0, cut));
            buf = buf.slice(cut + 2);
          }
        }
      } catch {
        /* dropped — retry below; since_seq repairs the gap */
      }
      this.handlers.onStatus(false);
      if (!this.stopped) await sleep(RETRY_MS);
    }
  }

  private handleFrame(frame: string): void {
    let id: number | null = null;
    let type: string | null = null;
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("id:")) id = parseInt(line.slice(3).trim(), 10);
      else if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (id !== null && Number.isInteger(id) && id > 0) this.lastSeq = id;
    if (type === "stream_reset") {
      this.lastSeq = 0; // ring overflowed: resubscribe from retained snapshot
      return;
    }
    if (!type || !data) return;
    let ev: Record<string, unknown>;
    try {
      ev = JSON.parse(data) as Record<string, unknown>;
    } catch {
      return;
    }
    this.handlers.onEvent(type, ev);
  }
}
