<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { DeviceStatusView, PluginStatusView, RunResult, RunStateEvent } from "../api/contracts";
import MetricCard from "../components/MetricCard.vue";
import StatusChip from "../components/StatusChip.vue";
import PreviewCanvas from "../components/PreviewCanvas.vue";
import LogConsole from "../components/LogConsole.vue";
import { apiClient, connection } from "../stores/connection";
import { clearLogs, events, onBusEvent } from "../stores/events";
import { fmtNum, shortId, timeAgo, toneOf } from "../lib/format";

const devices = ref<DeviceStatusView[]>([]);
const runs = ref<RunResult[]>([]);
const plugins = ref<PluginStatusView[]>([]);

async function load(): Promise<void> {
  const c = apiClient();
  const [d, r, p] = await Promise.all([c.devices(), c.runs(200), c.plugins()]);
  devices.value = d.data;
  runs.value = r.data;
  plugins.value = p.data;
}

const readyCount = computed(
  () =>
    devices.value.filter((d) => {
      const health = events.deviceHealth[d.instrument_id];
      const state = health ? (health.status === "ok" ? "ready" : health.status) : d.lifecycle;
      return state === "ready" || state === "ok";
    }).length,
);

const pluginsLoaded = computed(() => plugins.value.filter((p) => p.state === "loaded").length);
const pluginsFailed = computed(() => plugins.value.filter((p) => p.state === "failed").length);

const activeRuns = computed(() =>
  Object.entries(events.runStates)
    .filter(([, ev]) => !ev.final)
    .map(([taskId, ev]) => ({ taskId, ev: ev as RunStateEvent, prog: events.progress[taskId] })),
);

const recentRuns = computed(() => runs.value.slice(0, 6));
const completedCount = computed(() => runs.value.filter((r) => r.state === "completed").length);

let unsub: (() => void) | null = null;
onMounted(() => {
  void load();
  unsub = onBusEvent((type, ev) => {
    if (type === "run_state" && (ev as { final?: boolean }).final) void load();
  });
});
onBeforeUnmount(() => unsub?.());

function pct(step: number, total: number | null | undefined): number {
  return total ? Math.min(100, ((step + 1) / total) * 100) : 0;
}
</script>

<template>
  <div class="page">
  <div class="metrics">
    <MetricCard
      icon="devices"
      label="Devices ready"
      :value="`${readyCount} / ${devices.length}`"
      :tone="readyCount === devices.length && devices.length > 0 ? 'ok' : 'warn'"
    />
    <MetricCard
      icon="play_circle"
      label="Active runs"
      :value="String(activeRuns.length)"
      tone="info"
    />
    <MetricCard
      icon="layers"
      label="Runs recorded"
      :value="String(runs.length)"
      :sub="`${completedCount} completed`"
    />
    <MetricCard
      icon="puzzle"
      label="Plugins"
      :value="String(pluginsLoaded)"
      :sub="pluginsFailed ? `${pluginsFailed} failed` : 'all healthy'"
      :tone="pluginsFailed ? 'bad' : 'ok'"
    />
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">
        Live activity
        <span class="spacer" />
        <span class="dim small">server v{{ connection.meta?.app_version }} · api v{{ connection.meta?.api_version }}</span>
      </div>
      <div class="card-pad">
        <div v-if="!activeRuns.length" class="empty">
          no active run — start one from Run Control
        </div>
        <div v-for="r in activeRuns" :key="r.taskId" class="active-run">
          <div class="ar-head">
            <span class="mono">{{ shortId(r.taskId, 28) }}</span>
            <StatusChip :state="r.ev.state" :pulse="r.ev.state === 'running'" />
          </div>
          <div v-if="r.prog" class="ar-prog">
            <div class="progress">
              <div :style="{ width: pct(r.prog.step, r.prog.total) + '%' }" />
            </div>
            <span class="dim small">
              step {{ r.prog.step }}<template v-if="r.prog.total"> / {{ r.prog.total }}</template>
              <template v-for="(v, k) in r.prog.metrics ?? {}" :key="k">
                · {{ k }} = {{ fmtNum(v) }}
              </template>
            </span>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Live preview</div>
      <div class="card-pad">
        <PreviewCanvas :preview="events.lastPreview?.preview ?? null" :height="180" />
      </div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">Recent runs</div>
      <table class="data">
        <thead>
          <tr><th>run</th><th>command</th><th>state</th><th>when</th></tr>
        </thead>
        <tbody>
          <tr v-if="!recentRuns.length"><td colspan="4" class="empty">no runs yet</td></tr>
          <tr v-for="r in recentRuns" :key="r.run_id">
            <td class="mono" :title="r.run_id">{{ shortId(r.run_id, 24) }}</td>
            <td>{{ r.command }}</td>
            <td><span class="chip" :class="toneOf(r.state)">{{ r.state }}</span></td>
            <td class="dim">{{ timeAgo(r.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Event log</div>
      <div class="card-pad">
        <LogConsole :lines="events.logs.slice(-120)" height="200px" :toolbar="false" @clear="clearLogs" />
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
}
.two-col {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: 14px;
  align-items: start;
}
@media (max-width: 1100px) { .two-col { grid-template-columns: 1fr; } }
.active-run { padding: 8px 0; border-bottom: 1px solid var(--border); }
.active-run:last-child { border-bottom: none; }
.ar-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.ar-prog { margin-top: 7px; display: flex; flex-direction: column; gap: 4px; }
</style>
