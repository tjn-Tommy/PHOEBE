<script setup lang="ts">
import { ref } from "vue";
import AppIcon from "../components/AppIcon.vue";
import StatusChip from "../components/StatusChip.vue";
import {
  connection,
  connect,
  disconnect,
  PINNED_CONTRACTS_VERSION,
} from "../stores/connection";
import { theme, setTheme, type ThemeMode } from "../stores/theme";

const url = ref(connection.url);
const token = ref(connection.token);
const busy = ref(false);

async function reconnect(): Promise<void> {
  busy.value = true;
  try {
    await connect(url.value.trim(), token.value.trim());
  } catch {
    /* connection.error shows it */
  } finally {
    busy.value = false;
  }
}

const MODES: { mode: ThemeMode; icon: string; label: string }[] = [
  { mode: "light", icon: "sun", label: "Light" },
  { mode: "dark", icon: "moon", label: "Dark" },
  { mode: "system", icon: "settings", label: "System" },
];
</script>

<template>
  <div class="stack">
    <div class="card">
      <div class="card-title">Connection</div>
      <div class="card-pad col">
        <label class="field-label" for="s-url">Server URL</label>
        <input id="s-url" v-model="url" class="field" spellcheck="false" />
        <label class="field-label" for="s-token">Session token</label>
        <input id="s-token" v-model="token" class="field" type="password" spellcheck="false" />
        <div class="row">
          <button class="btn primary sm" :disabled="busy" @click="reconnect">
            <AppIcon name="zap" :size="13" /> {{ busy ? "connecting…" : "reconnect" }}
          </button>
          <button class="btn sm" @click="disconnect">
            <AppIcon name="power" :size="13" /> disconnect
          </button>
          <span class="spacer" />
          <StatusChip :state="connection.status === 'connected' ? 'ok' : 'offline'" />
        </div>
        <div v-if="connection.error" class="alert bad">{{ connection.error }}</div>
      </div>
    </div>

    <div class="card" v-if="connection.meta">
      <div class="card-title">Server</div>
      <div class="card-pad col">
        <div class="kv"><span>app version</span><span class="mono">{{ connection.meta.app_version }}</span></div>
        <div class="kv"><span>api version</span><span class="mono">v{{ connection.meta.api_version }}</span></div>
        <div class="kv">
          <span>contracts</span>
          <span>
            <span class="mono">v{{ connection.meta.contracts_version }}</span>
            <span
              class="chip"
              :class="connection.meta.contracts_version === PINNED_CONTRACTS_VERSION ? 'ok' : 'warn'"
              style="margin-left: 8px"
            >
              client pins v{{ PINNED_CONTRACTS_VERSION }}
            </span>
          </span>
        </div>
        <div class="kv"><span>role</span><span><StatusChip :state="connection.meta.role" /></span></div>
        <div class="kv"><span>web ui</span><span><StatusChip :state="connection.meta.static_ui" /></span></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Appearance</div>
      <div class="card-pad row">
        <button
          v-for="m in MODES"
          :key="m.mode"
          class="btn"
          :class="{ primary: theme.mode === m.mode }"
          @click="setTheme(m.mode)"
        >
          <AppIcon :name="m.icon" :size="14" /> {{ m.label }}
        </button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">About</div>
      <div class="card-pad dim small">
        PHOEBE desktop client v0.1.0 — Vue 3 + Tauri 2, speaking the
        <span class="mono">/api/v1</span> envelope contract with SSE live events.
        The Python backend, PyQt5 panel, zero-build web client and this app all
        share the same services surface.
      </div>
    </div>
  </div>
</template>

<style scoped>
.stack { max-width: 640px; display: flex; flex-direction: column; gap: 14px; }
.col { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; align-items: center; gap: 8px; }
.kv { display: flex; gap: 10px; font-size: 12.5px; align-items: center; }
.kv > span:first-child { width: 110px; flex: none; color: var(--dim); }
</style>
