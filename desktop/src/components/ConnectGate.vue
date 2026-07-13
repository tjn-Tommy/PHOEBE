<script setup lang="ts">
import { ref } from "vue";
import { connection, connect } from "../stores/connection";

const url = ref(connection.url);
const token = ref(connection.token);
const showToken = ref(false);
const busy = ref(false);

async function go(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await connect(url.value.trim(), token.value.trim());
  } catch {
    /* connection.error carries the message */
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="gate">
    <div class="halo" />
    <div class="card panel">
      <div class="head">
        <img src="/phoebe.svg" alt="" class="logo" />
        <div class="title">PHOEBE</div>
        <div class="sub">Photonic Hardware Orchestrator for Extensible Benchtop Experiments</div>
      </div>

      <label class="field-label" for="url">Server</label>
      <input
        id="url"
        v-model="url"
        class="field"
        spellcheck="false"
        placeholder="http://127.0.0.1:8760"
        @keydown.enter="go"
      />

      <label class="field-label tok" for="token">Session token</label>
      <div class="tok-row">
        <input
          id="token"
          v-model="token"
          class="field"
          :type="showToken ? 'text' : 'password'"
          spellcheck="false"
          placeholder="printed when the server starts"
          @keydown.enter="go"
        />
        <button class="btn ghost sm" type="button" @click="showToken = !showToken">
          {{ showToken ? "hide" : "show" }}
        </button>
      </div>

      <button class="btn primary connect" :disabled="busy" @click="go">
        {{ busy ? "connecting…" : "Connect" }}
      </button>

      <div v-if="connection.error" class="alert bad">{{ connection.error }}</div>

      <p class="hint">
        Start the backend with
        <code>python -m phoebe.server --config config/sim.toml</code> — the
        per-process token is printed to its console (loopback binds only).
      </p>
    </div>
  </div>
</template>

<style scoped>
.gate {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  position: relative; overflow: hidden; background: var(--bg);
}
.halo {
  position: absolute; width: 720px; height: 720px; border-radius: 50%;
  background: radial-gradient(circle,
    color-mix(in srgb, #8b5cf6 16%, transparent) 0%,
    color-mix(in srgb, #22d3ee 8%, transparent) 45%,
    transparent 70%);
  filter: blur(6px);
  pointer-events: none;
}
.panel {
  position: relative; width: 380px; padding: 30px 32px 24px;
  display: flex; flex-direction: column; gap: 6px;
  box-shadow: var(--shadow-lg);
}
.head { text-align: center; margin-bottom: 14px; }
.logo { width: 52px; height: 52px; }
.title {
  font-size: 22px; font-weight: 800; letter-spacing: .18em; margin-top: 6px;
  background: var(--grad); -webkit-background-clip: text; background-clip: text;
  color: transparent;
}
.sub { font-size: 11px; color: var(--dim); margin-top: 3px; }
.tok { margin-top: 10px; }
.tok-row { display: flex; gap: 6px; align-items: center; }
.connect { margin-top: 16px; padding: 9px; }
.alert { margin-top: 10px; }
.hint { margin-top: 14px; font-size: 11px; color: var(--dim); line-height: 1.5; }
.hint code {
  font-family: var(--mono); font-size: 10.5px;
  background: var(--hover); border-radius: 4px; padding: 1px 4px;
}
</style>
