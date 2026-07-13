<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import type { RunJournalRecord, RunResult } from "../api/contracts";
import AppIcon from "../components/AppIcon.vue";
import StatusChip from "../components/StatusChip.vue";
import { apiClient } from "../stores/connection";
import { onBusEvent } from "../stores/events";
import { fmtClock, fmtTime, shortId, toneOf } from "../lib/format";

const rows = ref<RunResult[]>([]);
const selected = ref<RunResult | null>(null);
const journal = ref<RunJournalRecord[]>([]);

async function load(): Promise<void> {
  rows.value = (await apiClient().runs(200)).data;
  if (selected.value) {
    const still = rows.value.find((r) => r.run_id === selected.value?.run_id);
    if (still) await select(still);
  }
}

async function select(r: RunResult): Promise<void> {
  selected.value = r;
  journal.value = (await apiClient().runJournal(r.run_id)).data;
}

function recTone(rec: RunJournalRecord): string {
  if (rec.record === "finalized") return rec.finalized === "ok" ? "ok" : "warn";
  if (rec.record === "execution_outcome") {
    return rec.outcome === "completed" ? "ok" : "bad";
  }
  if (rec.record === "recovered") return "warn";
  return "info";
}

let unsub: (() => void) | null = null;
onMounted(() => {
  void load();
  unsub = onBusEvent((type, ev) => {
    if (type === "run_state" && (ev as { final?: boolean }).final) void load();
  });
});
onBeforeUnmount(() => unsub?.());
</script>

<template>
  <div class="page">
  <div class="toolbar">
    <button class="btn sm" @click="load"><AppIcon name="refresh" :size="14" /> refresh</button>
    <span class="dim small">{{ rows.length }} runs in catalog</span>
  </div>

  <div class="split">
    <div class="card list">
      <table class="data">
        <thead>
          <tr><th>run</th><th>command</th><th>state</th><th>finalized</th><th>created</th></tr>
        </thead>
        <tbody>
          <tr v-if="!rows.length"><td colspan="5" class="empty">no runs recorded yet</td></tr>
          <tr
            v-for="r in rows"
            :key="r.run_id"
            :class="{ selected: selected?.run_id === r.run_id }"
            style="cursor: pointer"
            @click="select(r)"
          >
            <td class="mono" :title="r.run_id">{{ shortId(r.run_id, 26) }}</td>
            <td>{{ r.command }}</td>
            <td><span class="chip" :class="toneOf(r.state)">{{ r.state }}</span></td>
            <td>
              <span v-if="r.finalized" class="chip" :class="r.finalized === 'ok' ? 'ok' : 'degraded'">
                {{ r.finalized }}
              </span>
              <span v-else class="dim">—</span>
            </td>
            <td class="dim small">{{ fmtTime(r.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="selected" class="card detail">
      <div class="card-title">
        Run detail
        <span class="spacer" />
        <button class="btn ghost sm icon-only" @click="selected = null">
          <AppIcon name="x" :size="14" />
        </button>
      </div>
      <div class="card-pad col">
        <div class="kv"><span>run_id</span><span class="mono">{{ selected.run_id }}</span></div>
        <div class="kv"><span>plugin</span><span class="mono">{{ selected.plugin_id }}</span></div>
        <div class="kv"><span>command</span><span class="mono">{{ selected.command }}</span></div>
        <div class="kv"><span>created</span><span>{{ fmtTime(selected.created_at) }}</span></div>
        <div class="kv">
          <span>state</span>
          <span><StatusChip :state="selected.state" /></span>
        </div>
        <div class="kv" v-if="selected.execution_outcome">
          <span>outcome</span>
          <span><StatusChip :state="selected.execution_outcome" /></span>
        </div>
        <div class="kv"><span>run dir</span><span class="mono small">{{ selected.run_dir }}</span></div>

        <div class="jr-title">Journal</div>
        <div class="journal">
          <div v-for="(rec, i) in journal" :key="i" class="jr">
            <span class="jr-t mono">{{ fmtClock(rec.t_wall) }}</span>
            <span class="chip" :class="recTone(rec)">{{ rec.record }}</span>
            <span class="jr-x dim small">
              <template v-if="rec.outcome">{{ rec.outcome }}</template>
              <template v-if="rec.finalized"> {{ rec.finalized }}</template>
              <template v-if="rec.resolution"> {{ rec.resolution }}</template>
              {{ rec.detail ?? "" }}
            </span>
          </div>
          <div v-if="!journal.length" class="empty">journal is empty</div>
        </div>
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 10px; }
.split { display: grid; grid-template-columns: 3fr 2fr; gap: 14px; align-items: start; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
.list { overflow: hidden; }
.col { display: flex; flex-direction: column; gap: 7px; }
.kv { display: flex; gap: 10px; font-size: 12.5px; }
.kv > span:first-child { width: 74px; flex: none; color: var(--dim); }
.kv > span:last-child { min-width: 0; word-break: break-all; }
.jr-title {
  margin-top: 10px; font-size: 11.5px; font-weight: 650; color: var(--accent);
  text-transform: uppercase; letter-spacing: .05em;
}
.journal { display: flex; flex-direction: column; gap: 5px; }
.jr { display: flex; align-items: baseline; gap: 8px; }
.jr-t { flex: none; font-size: 10.5px; color: var(--dim); }
.jr-x { min-width: 0; word-break: break-word; }
</style>
