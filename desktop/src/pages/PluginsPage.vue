<script setup lang="ts">
import { onMounted, ref } from "vue";
import type { PluginStatusView } from "../api/contracts";
import AppIcon from "../components/AppIcon.vue";
import StatusChip from "../components/StatusChip.vue";
import { apiClient, isOperator } from "../stores/connection";

const rows = ref<PluginStatusView[]>([]);
const busyId = ref("");
const msg = ref("");
const expanded = ref<Record<string, boolean>>({});

async function load(): Promise<void> {
  rows.value = (await apiClient().plugins()).data;
}

async function toggle(p: PluginStatusView): Promise<void> {
  busyId.value = p.plugin_id;
  try {
    if (p.state === "disabled") await apiClient().enablePlugin(p.plugin_id);
    else await apiClient().disablePlugin(p.plugin_id);
    msg.value = `${p.state === "disabled" ? "enabled" : "disabled"} ${p.plugin_id}`;
    await load();
  } catch (e) {
    msg.value = `toggle failed: ${e instanceof Error ? e.message : e}`;
  } finally {
    busyId.value = "";
  }
}

onMounted(load);
</script>

<template>
  <div class="page">
  <div class="toolbar">
    <button class="btn sm" @click="load"><AppIcon name="refresh" :size="14" /> refresh</button>
    <span class="dim small">
      plugin folders = <code class="mono">plugin.toml</code> + <code class="mono">plugin.py</code>;
      a broken plugin degrades, never aborts startup
    </span>
    <span class="spacer" />
    <span v-if="msg" class="dim small mono">{{ msg }}</span>
  </div>

  <div class="grid">
    <div v-for="p in rows" :key="p.plugin_id + p.state" class="card card-pad plugin">
      <div class="head">
        <div class="p-icon" :class="{ failed: p.state === 'failed' }">
          <AppIcon :name="p.state === 'failed' ? 'alert' : 'puzzle'" :size="19" />
        </div>
        <div class="ident">
          <div class="pid mono">{{ p.plugin_id }}</div>
          <div class="dim small">
            v{{ p.version || "?" }}
            <template v-if="p.api"> · api {{ p.api }}</template>
          </div>
        </div>
        <StatusChip :state="p.state" />
      </div>

      <div v-if="p.commands?.length" class="chips">
        <span v-for="c in p.commands" :key="c" class="chip accent mono">{{ c }}</span>
      </div>

      <div class="dim small mono src">{{ p.source }}</div>

      <div v-if="p.error" class="alert bad small">
        {{ p.error.message }}
      </div>
      <template v-if="p.detail">
        <button
          v-if="p.state === 'failed'"
          class="btn ghost sm trace-btn"
          @click="expanded[p.plugin_id] = !expanded[p.plugin_id]"
        >
          {{ expanded[p.plugin_id] ? "hide traceback" : "show traceback" }}
        </button>
        <pre v-if="p.state !== 'failed' || expanded[p.plugin_id]" class="detail mono">{{ p.detail }}</pre>
      </template>

      <div class="actions" v-if="p.state !== 'failed'">
        <button
          class="btn sm"
          :class="{ danger: p.state === 'loaded' }"
          :disabled="!isOperator || busyId === p.plugin_id"
          @click="toggle(p)"
        >
          <AppIcon :name="p.state === 'disabled' ? 'check' : 'x'" :size="13" />
          {{ p.state === "disabled" ? "enable" : "disable" }}
        </button>
      </div>
    </div>
    <div v-if="!rows.length" class="empty">no plugins registered</div>
  </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 10px; }
.toolbar code { background: var(--hover); border-radius: 4px; padding: 0 4px; font-size: 11px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 14px;
}
.plugin { display: flex; flex-direction: column; gap: 10px; }
.head { display: flex; align-items: center; gap: 11px; }
.p-icon {
  width: 40px; height: 40px; border-radius: 11px; flex: none;
  background: var(--grad-soft); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
}
.p-icon.failed {
  background: color-mix(in srgb, var(--bad) 11%, transparent);
  color: var(--bad);
}
.ident { flex: 1; min-width: 0; }
.pid { font-weight: 650; font-size: 13px; overflow: hidden; text-overflow: ellipsis; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.src { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10.5px; }
.trace-btn { align-self: flex-start; }
.detail {
  margin: 0; font-size: 10.5px; color: var(--dim);
  background: var(--card-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px;
  max-height: 180px; overflow: auto; white-space: pre-wrap; word-break: break-word;
}
.actions { display: flex; gap: 7px; }
</style>
