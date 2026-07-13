<script setup lang="ts">
import LogConsole from "../components/LogConsole.vue";
import { clearLogs, events } from "../stores/events";
</script>

<template>
  <div class="card card-pad">
    <LogConsole
      :lines="events.logs"
      height="calc(100vh - 190px)"
      @clear="clearLogs"
    />
  </div>
</template>
