<script setup lang="ts">
import { useRoute } from "vue-router";
import AppIcon from "./AppIcon.vue";
import { connection } from "../stores/connection";
import { events } from "../stores/events";

const route = useRoute();

const NAV = [
  { to: "/", icon: "dashboard", label: "Dashboard" },
  { to: "/run", icon: "play_circle", label: "Run Control" },
  { to: "/devices", icon: "devices", label: "Devices" },
  { to: "/runs", icon: "layers", label: "Runs" },
  { to: "/plugins", icon: "puzzle", label: "Plugins" },
  { to: "/logs", icon: "terminal", label: "Logs" },
  { to: "/settings", icon: "settings", label: "Settings" },
];

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <img src="/phoebe.svg" alt="" class="logo" />
      <div>
        <div class="name">PHOEBE</div>
        <div class="tag">orchestrator</div>
      </div>
    </div>

    <nav>
      <router-link
        v-for="item in NAV"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        :class="{ active: route.path === item.to }"
      >
        <AppIcon :name="item.icon" :size="17" />
        <span>{{ item.label }}</span>
      </router-link>
    </nav>

    <div class="foot">
      <span class="dot" :class="{ on: events.connected }" />
      <span class="host">{{ hostOf(connection.url) }}</span>
      <span v-if="connection.meta" class="role">{{ connection.meta.role }}</span>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 216px; flex: none;
  background: var(--sidebar-bg);
  display: flex; flex-direction: column;
  padding: 18px 12px 14px;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px 18px; }
.logo { width: 34px; height: 34px; }
.name {
  font-size: 16px; font-weight: 800; letter-spacing: .14em;
  background: var(--grad); -webkit-background-clip: text; background-clip: text;
  color: transparent;
}
.tag { font-size: 10px; color: var(--sidebar-text); letter-spacing: .22em; text-transform: uppercase; }

nav { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border-radius: 9px;
  color: var(--sidebar-text); font-size: 13px; font-weight: 550;
  transition: all .13s ease;
}
.nav-item:hover { color: var(--sidebar-active); background: rgba(255, 255, 255, .05); }
.nav-item.active {
  color: var(--sidebar-active);
  background: linear-gradient(135deg, rgba(139, 92, 246, .28), rgba(34, 211, 238, .16));
  box-shadow: inset 0 0 0 1px rgba(139, 92, 246, .30);
}

.foot {
  display: flex; align-items: center; gap: 7px;
  padding: 10px 8px 0; border-top: 1px solid rgba(255, 255, 255, .07);
  font-size: 11px; color: var(--sidebar-text);
}
.dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--bad); flex: none;
  box-shadow: 0 0 6px color-mix(in srgb, var(--bad) 60%, transparent);
}
.dot.on { background: var(--ok); box-shadow: 0 0 6px color-mix(in srgb, var(--ok) 70%, transparent); }
.host { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; font-family: var(--mono); }
.role {
  font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
  padding: 1px 6px; border-radius: 5px;
  background: rgba(255, 255, 255, .08); color: var(--sidebar-active);
}
</style>
