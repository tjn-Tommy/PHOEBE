/* Typed client for the PHOEBE HTTP adapter (/api/v1).
 *
 * Envelope discipline mirrors the server contract: `status: "error"` becomes
 * a thrown PhoebeApiError (transport-level), while domain rejections arrive
 * as CommandAcks inside `data` with `status: "warning"` — callers branch on
 * `ack.code`, never on prose. */
import type {
  ApiEnvelope,
  CommandAck,
  CommandEnvelope,
  ControllerStats,
  DeviceStatusView,
  EventBusStats,
  PluginStatusView,
  RunJournalRecord,
  RunResult,
  ServerMeta,
} from "./contracts";

export class PhoebeApiError extends Error {
  constructor(
    message: string,
    readonly code: string = "internal",
    readonly status: number = 0,
  ) {
    super(message);
    this.name = "PhoebeApiError";
  }
}

export interface Warned<T> {
  data: T;
  warning: string | null;
}

export class PhoebeClient {
  constructor(
    public baseUrl: string,
    public token: string,
  ) {}

  get apiRoot(): string {
    return this.baseUrl.replace(/\/+$/, "") + "/api/v1";
  }

  headers(json = false): Record<string, string> {
    return {
      authorization: `Bearer ${this.token}`,
      ...(json ? { "content-type": "application/json" } : {}),
    };
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<Warned<T>> {
    let res: Response;
    try {
      res = await fetch(this.apiRoot + path, {
        ...init,
        headers: { ...this.headers(Boolean(init.body)), ...(init.headers ?? {}) },
      });
    } catch {
      throw new PhoebeApiError(`cannot reach ${this.baseUrl}`, "unreachable");
    }
    let body: ApiEnvelope;
    try {
      body = (await res.json()) as ApiEnvelope;
    } catch {
      throw new PhoebeApiError(`bad response (HTTP ${res.status})`, "internal", res.status);
    }
    if (body.status === "error") {
      const err = body.error;
      throw new PhoebeApiError(
        err?.message ?? "request failed",
        err?.code ?? "internal",
        err?.status ?? res.status,
      );
    }
    return { data: body.data as T, warning: body.warning ?? null };
  }

  private get<T>(path: string): Promise<Warned<T>> {
    return this.request<T>(path);
  }

  private post<T>(path: string, payload?: unknown): Promise<Warned<T>> {
    return this.request<T>(path, {
      method: "POST",
      ...(payload !== undefined ? { body: JSON.stringify(payload) } : {}),
    });
  }

  /* ------------------------------------------------------------- surface */
  meta = () => this.get<ServerMeta>("/meta");
  devices = () => this.get<DeviceStatusView[]>("/devices");
  deviceStats = () => this.get<Record<string, ControllerStats>>("/devices/stats");
  reconnectDevice = (id: string) => this.post<boolean>(`/devices/${encodeURIComponent(id)}/reconnect`);
  disableDevice = (id: string) => this.post<null>(`/devices/${encodeURIComponent(id)}/disable`);
  healthCheckAll = () => this.post<null>("/devices/health-check");

  commands = () => this.get<string[]>("/plugins/commands");
  commandSchema = (command: string) =>
    this.get<Record<string, unknown>>(`/plugins/commands/${encodeURIComponent(command)}/schema`);
  plugins = () => this.get<PluginStatusView[]>("/plugins");
  enablePlugin = (id: string) => this.post<null>(`/plugins/${encodeURIComponent(id)}/enable`);
  disablePlugin = (id: string) => this.post<null>(`/plugins/${encodeURIComponent(id)}/disable`);

  submit = (envelope: CommandEnvelope) => this.post<CommandAck>("/commands", envelope);
  pause = (taskId: string) => this.post<CommandAck>(`/runs/${encodeURIComponent(taskId)}/pause`);
  resume = (taskId: string) => this.post<CommandAck>(`/runs/${encodeURIComponent(taskId)}/resume`);
  cancel = (taskId: string) => this.post<CommandAck>(`/runs/${encodeURIComponent(taskId)}/cancel`);

  runs = (limit = 100, offset = 0) => this.get<RunResult[]>(`/runs?limit=${limit}&offset=${offset}`);
  run = (runId: string) => this.get<RunResult | null>(`/runs/${encodeURIComponent(runId)}`);
  runJournal = (runId: string) =>
    this.get<RunJournalRecord[]>(`/runs/${encodeURIComponent(runId)}/journal`);
  activeTasks = () => this.get<string[]>("/tasks");
  busStats = () => this.get<EventBusStats>("/events/stats");
}
