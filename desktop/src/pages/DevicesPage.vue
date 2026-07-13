<script setup lang="ts">
import { onMounted, ref } from "vue";
import type { DeviceStatusView } from "../api/contracts";
import AppIcon from "../components/AppIcon.vue";
import StatusChip from "../components/StatusChip.vue";
import { apiClient, isOperator } from "../stores/connection";
import { events } from "../stores/events";
import { timeAgo } from "../lib/format";

const devices = ref<DeviceStatusView[]>([]);
const msg = ref("");
const busyId = ref("");

const KIND_ICON: Record<string, string> = {
  pattern_modulator: "grid",
  spectrum_analyzer: "wave",
  oscilloscope: "scope",
  analog_input: "bars",
  waveform_generator: "square_wave",
};

async function load(): Promise<void> {
  devices.value = (await apiClient().devices()).data;
}

function effectiveState(d: DeviceStatusView): string {
  const health = events.deviceHealth[d.instrument_id];
  if (!health) return d.lifecycle;
  return health.status === "ok" ? "ready" : health.status;
}

async function act(
  d: DeviceStatusView,
  action: "reconnect" | "disable",
): Promise<void> {
  busyId.value = d.instrument_id;
  try {
    if (action === "reconnect") await apiClient().reconnectDevice(d.instrument_id);
    else await apiClient().disableDevice(d.instrument_id);
    msg.value = `${action} → ${d.instrument_id}`;
    await load();
  } catch (e) {
    msg.value = `${action} ${d.instrument_id} failed: ${e instanceof Error ? e.message : e}`;
  } finally {
    busyId.value = "";
  }
}

async function healthCheckAll(): Promise<void> {
  try {
    await apiClient().healthCheckAll();
    msg.value = "health check requested for all devices";
    await load();
  } catch (e) {
    msg.value = `health check failed: ${e instanceof Error ? e.message : e}`;
  }
}

onMounted(load);
</script>

<template>
  <div class="page">
  <div class="toolbar">
    <button class="btn sm" @click="load"><AppIcon name="refresh" :size="14" /> refresh</button>
    <button class="btn sm" :disabled="!isOperator" @click="healthCheckAll">
      <AppIcon name="check" :size="14" /> health-check all
    </button>
    <span class="spacer" />
    <span v-if="msg" class="dim small mono">{{ msg }}</span>
  </div>

  <div class="grid">
    <div v-for="d in devices" :key="d.instrument_id" class="card card-pad device">
      <div class="head">
        <div class="d-icon"><AppIcon :name="KIND_ICON[d.kind] ?? 'devices'" :size="20" /></div>
        <div class="ident">
          <div class="iid mono">{{ d.instrument_id }}</div>
          <div class="dim small">{{ d.vendor }} · {{ d.model }}</div>
        </div>
        <StatusChip :state="effectiveState(d)" :pulse="effectiveState(d) === 'connecting'" />
      </div>

      <div class="chips">
        <span class="chip accent">{{ d.role }}</span>
        <span class="chip" :class="d.backend === 'sim' ? 'info' : 'warn'">{{ d.backend }}</span>
        <span class="chip">{{ d.kind }}</span>
      </div>

      <div v-if="d.detail" class="dim small detail">{{ d.detail }}</div>
      <div v-if="d.stats" class="dim small stats mono">
        ops {{ d.stats.ops_ok ?? 0 }} ok / {{ d.stats.ops_failed ?? 0 }} failed
        · up since {{ timeAgo(d.stats.started_at) }}
      </div>

      <div class="actions">
        <button
          class="btn sm"
          :disabled="!isOperator || busyId === d.instrument_id"
          @click="act(d, 'reconnect')"
        >
          <AppIcon name="refresh" :size="13" /> reconnect
        </button>
        <button
          class="btn sm danger"
          :disabled="!isOperator || busyId === d.instrument_id"
          @click="act(d, 'disable')"
        >
          <AppIcon name="power" :size="13" /> disable
        </button>
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}
.device { display: flex; flex-direction: column; gap: 10px; }
.head { display: flex; align-items: center; gap: 11px; }
.d-icon {
  width: 40px; height: 40px; border-radius: 11px; flex: none;
  background: var(--grad-soft); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
}
.ident { flex: 1; min-width: 0; }
.iid { font-weight: 650; font-size: 13px; overflow: hidden; text-overflow: ellipsis; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.detail { word-break: break-word; }
.actions { display: flex; gap: 7px; margin-top: 2px; }
</style>
