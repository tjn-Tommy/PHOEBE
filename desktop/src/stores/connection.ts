import { computed, reactive } from "vue";
import { PhoebeClient, PhoebeApiError } from "../api/client";
import type { ServerMeta } from "../api/contracts";
import { startEventStream, stopEventStream } from "./events";

/** The contracts version this client was built against (A14 pin). */
export const PINNED_CONTRACTS_VERSION = 2;

export const connection = reactive({
  url: localStorage.getItem("phoebe.url") ?? "http://127.0.0.1:8760",
  token: localStorage.getItem("phoebe.token") ?? "",
  status: "disconnected" as "disconnected" | "connecting" | "connected",
  error: "",
  meta: null as ServerMeta | null,
});

let client = new PhoebeClient(connection.url, connection.token);

export function apiClient(): PhoebeClient {
  return client;
}

export const isOperator = computed(
  () => connection.meta?.role === "operator",
);

export const contractsSkew = computed(
  () =>
    connection.meta !== null &&
    connection.meta.contracts_version !== PINNED_CONTRACTS_VERSION,
);

export async function connect(url: string, token: string): Promise<void> {
  connection.status = "connecting";
  connection.error = "";
  client = new PhoebeClient(url, token);
  try {
    const { data: meta } = await client.meta();
    connection.url = url;
    connection.token = token;
    connection.meta = meta;
    connection.status = "connected";
    localStorage.setItem("phoebe.url", url);
    localStorage.setItem("phoebe.token", token);
    startEventStream(client);
  } catch (e) {
    connection.status = "disconnected";
    connection.error =
      e instanceof PhoebeApiError
        ? e.code === "unauthorized"
          ? "invalid or expired session token"
          : e.message
        : String(e);
    throw e;
  }
}

export function disconnect(): void {
  stopEventStream();
  connection.status = "disconnected";
  connection.meta = null;
}
