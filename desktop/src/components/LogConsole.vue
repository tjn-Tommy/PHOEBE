<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { LogLine } from "../stores/events";
import { fmtClock } from "../lib/format";

const props = withDefaults(
  defineProps<{ lines: LogLine[]; height?: string; toolbar?: boolean }>(),
  { height: "280px", toolbar: true },
);
const emit = defineEmits<{ clear: [] }>();

const LEVELS = ["all", "debug", "info", "warning", "error"] as const;
const filter = ref<(typeof LEVELS)[number]>("all");
const autoScroll = ref(true);
const body = ref<HTMLDivElement | null>(null);

const visible = computed(() =>
  filter.value === "all"
    ? props.lines
    : props.lines.filter((l) => l.level === filter.value),
);

watch(
  () => props.lines.length,
  async () => {
    if (!autoScroll.value) return;
    await nextTick();
    body.value?.scrollTo({ top: body.value.scrollHeight });
  },
);
</script>

<template>
  <div class="console">
    <div v-if="props.toolbar" class="bar">
      <button
        v-for="l in LEVELS"
        :key="l"
        class="lvl"
        :class="{ active: filter === l, [l]: true }"
        @click="filter = l"
      >
        {{ l }}
      </button>
      <span class="spacer" />
      <label class="auto">
        <input v-model="autoScroll" type="checkbox" /> follow
      </label>
      <button class="btn ghost sm" @click="emit('clear')">clear</button>
    </div>
    <div ref="body" class="body" :style="{ height: props.height }">
      <div v-if="!visible.length" class="empty">no log lines yet</div>
      <div v-for="l in visible" :key="l.n" class="line">
        <span class="t">{{ fmtClock(l.t) }}</span>
        <span class="badge" :class="l.level">{{ l.level }}</span>
        <span class="msg">{{ l.message }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.console { display: flex; flex-direction: column; min-width: 0; }
.bar {
  display: flex; align-items: center; gap: 4px;
  padding: 0 0 8px;
}
.lvl {
  border: none; background: transparent; color: var(--dim);
  font: inherit; font-size: 11.5px; font-weight: 600; cursor: pointer;
  padding: 3px 9px; border-radius: 999px;
}
.lvl.active { background: var(--hover); color: var(--text); }
.lvl.active.error { color: var(--bad); }
.lvl.active.warning { color: var(--warn); }
.spacer { flex: 1; }
.auto { font-size: 11.5px; color: var(--dim); display: flex; align-items: center; gap: 4px; }
.body {
  overflow-y: auto; background: var(--input-bg);
  border: 1px solid var(--border); border-radius: 10px;
  padding: 8px 10px; font-family: var(--mono); font-size: 11.8px;
}
.line { display: flex; gap: 8px; padding: 1.5px 0; align-items: baseline; }
.t { color: var(--dim); flex: none; font-size: 10.5px; }
.badge {
  flex: none; width: 52px; text-align: center; border-radius: 5px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  background: var(--hover); color: var(--dim);
}
.badge.error { background: color-mix(in srgb, var(--bad) 15%, transparent); color: var(--bad); }
.badge.warning { background: color-mix(in srgb, var(--warn) 17%, transparent); color: var(--warn); }
.badge.info { background: color-mix(in srgb, var(--info) 13%, transparent); color: var(--info); }
.msg { white-space: pre-wrap; word-break: break-word; }
</style>
