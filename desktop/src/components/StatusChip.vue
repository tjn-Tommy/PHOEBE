<script setup lang="ts">
import { toneOf } from "../lib/format";

const props = defineProps<{ state: string; pulse?: boolean }>();
</script>

<template>
  <span class="chip" :class="toneOf(props.state)">
    <span v-if="props.pulse" class="pulse-dot" />{{ props.state }}
  </span>
</template>
