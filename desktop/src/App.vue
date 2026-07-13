<script setup lang="ts">
import { onMounted } from "vue";
import ConnectGate from "./components/ConnectGate.vue";
import Sidebar from "./components/Sidebar.vue";
import TopBar from "./components/TopBar.vue";
import { connection, connect } from "./stores/connection";

onMounted(async () => {
  // auto-reconnect with stored credentials; the gate shows on failure
  if (connection.token && connection.status === "disconnected") {
    try {
      await connect(connection.url, connection.token);
    } catch {
      /* gate stays up */
    }
  }
});
</script>

<template>
  <ConnectGate v-if="connection.status !== 'connected'" />
  <div v-else class="shell">
    <Sidebar />
    <div class="main">
      <TopBar />
      <div class="content">
        <div class="content-inner">
          <router-view v-slot="{ Component }">
            <transition name="page" mode="out-in">
              <component :is="Component" />
            </transition>
          </router-view>
        </div>
      </div>
    </div>
  </div>
</template>
