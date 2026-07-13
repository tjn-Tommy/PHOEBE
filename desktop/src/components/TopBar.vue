<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import AppIcon from "./AppIcon.vue";
import { connection, contractsSkew, disconnect, PINNED_CONTRACTS_VERSION } from "../stores/connection";
import { events } from "../stores/events";
import { resolvedTheme, setTheme } from "../stores/theme";

const route = useRoute();
const title = computed(() => (route.meta.title as string) ?? "PHOEBE");

function toggleTheme(): void {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}
</script>

<template>
  <header class="topbar">
    <h1>{{ title }}</h1>
    <span class="spacer" />

    <span v-if="contractsSkew" class="chip warn" :title="`client pins contracts v${PINNED_CONTRACTS_VERSION}, server speaks v${connection.meta?.contracts_version}`">
      <AppIcon name="alert" :size="13" /> contracts skew
    </span>

    <span class="stream" :class="{ on: events.connected }">
      <span class="s-dot" />
      {{ events.connected ? "live" : "offline" }}
      <span v-if="events.seen" class="seen">· {{ events.seen }} ev</span>
    </span>

    <button class="btn ghost icon-only" title="toggle theme" @click="toggleTheme">
      <AppIcon :name="resolvedTheme() === 'dark' ? 'sun' : 'moon'" :size="16" />
    </button>
    <button class="btn ghost icon-only" title="disconnect" @click="disconnect">
      <AppIcon name="power" :size="16" />
    </button>
  </header>
</template>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
}
h1 { font-size: 15.5px; font-weight: 700; }
.spacer { flex: 1; }
.stream {
  display: flex; align-items: center; gap: 6px;
  font-size: 11.5px; font-weight: 600; color: var(--dim);
}
.s-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--bad); }
.stream.on .s-dot { background: var(--ok); animation: pulse 2s ease-in-out infinite; }
.stream.on { color: var(--ok); }
.seen { color: var(--dim); font-weight: 500; }
</style>
