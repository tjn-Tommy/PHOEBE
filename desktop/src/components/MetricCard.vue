<script setup lang="ts">
import AppIcon from "./AppIcon.vue";

const props = defineProps<{
  icon: string;
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}>();
</script>

<template>
  <div class="card card-pad metric">
    <div class="m-icon" :class="props.tone ?? 'accent'">
      <AppIcon :name="props.icon" :size="20" />
    </div>
    <div class="m-body">
      <div class="m-value">{{ props.value }}</div>
      <div class="m-label">{{ props.label }}</div>
      <div v-if="props.sub" class="m-sub">{{ props.sub }}</div>
    </div>
  </div>
</template>

<style scoped>
.metric { display: flex; align-items: center; gap: 14px; }
.m-icon {
  width: 42px; height: 42px; border-radius: 12px; flex: none;
  display: flex; align-items: center; justify-content: center;
}
.m-icon.accent { background: var(--grad-soft); color: var(--accent); }
.m-icon.ok { background: color-mix(in srgb, var(--ok) 13%, transparent); color: var(--ok); }
.m-icon.info { background: color-mix(in srgb, var(--info) 13%, transparent); color: var(--info); }
.m-icon.warn { background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn); }
.m-icon.bad { background: color-mix(in srgb, var(--bad) 12%, transparent); color: var(--bad); }
.m-value { font-size: 20px; font-weight: 700; line-height: 1.15; }
.m-label { font-size: 12px; color: var(--dim); margin-top: 1px; }
.m-sub { font-size: 11px; color: var(--dim); opacity: .8; }
</style>
