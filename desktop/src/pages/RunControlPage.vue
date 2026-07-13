<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import type { CommandAck } from "../api/contracts";
import SchemaFormView from "../components/SchemaFormView.vue";
import StatusChip from "../components/StatusChip.vue";
import PreviewCanvas from "../components/PreviewCanvas.vue";
import AppIcon from "../components/AppIcon.vue";
import { apiClient, isOperator } from "../stores/connection";
import { events } from "../stores/events";
import { session } from "../stores/session";
import { buildFormModel, defaultsPayload, type FormGroup } from "../lib/schemaForm";
import { fmtNum, shortId } from "../lib/format";

const commands = ref<string[]>([]);
const selected = ref("");
const group = ref<FormGroup | null>(null);
const formState = ref<Record<string, unknown>>({});
const busy = ref(false);

async function loadSchema(command: string): Promise<void> {
  selected.value = command;
  const { data } = await apiClient().commandSchema(command);
  group.value = buildFormModel(data);
  formState.value = defaultsPayload(group.value);
}

function resetDefaults(): void {
  if (group.value) formState.value = defaultsPayload(group.value);
}

onMounted(async () => {
  const { data } = await apiClient().commands();
  commands.value = data;
  if (data.length) await loadSchema(data[0]);
});

function showAck(prefix: string, ack: CommandAck, warning: string | null): void {
  session.lastAck =
    `${prefix}: ${ack.code}` +
    (ack.task_id ? ` → ${shortId(ack.task_id, 30)}` : "") +
    (ack.reason ? ` (${ack.reason})` : "") +
    (warning ? ` — ${warning}` : "");
}

async function submit(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    const { data: ack, warning } = await apiClient().submit({
      command_id: crypto.randomUUID(), // ledger: a NEW id per attempt
      command: selected.value,
      payload: formState.value,
      issued_by: "desktop_ui",
    });
    showAck("submit", ack, warning);
    if (ack.accepted && ack.task_id) session.activeTaskId = ack.task_id;
  } catch (e) {
    session.lastAck = `submit failed: ${e instanceof Error ? e.message : e}`;
  } finally {
    busy.value = false;
  }
}

async function control(action: "pause" | "resume" | "cancel"): Promise<void> {
  if (!session.activeTaskId) return;
  try {
    const { data: ack, warning } = await apiClient()[action](session.activeTaskId);
    showAck(action, ack, warning);
  } catch (e) {
    session.lastAck = `${action} failed: ${e instanceof Error ? e.message : e}`;
  }
}

const runState = computed(() => events.runStates[session.activeTaskId] ?? null);
const progress = computed(() => events.progress[session.activeTaskId] ?? null);
const percent = computed(() => {
  const p = progress.value;
  return p?.total ? Math.min(100, ((p.step + 1) / p.total) * 100) : 0;
});
const canPause = computed(() =>
  ["running", "preparing"].includes(runState.value?.state ?? ""),
);
const canResume = computed(() => runState.value?.state === "paused");
const canCancel = computed(
  () => Boolean(session.activeTaskId) && !(runState.value?.final ?? false),
);
</script>

<template>
  <div class="page">
  <div v-if="!isOperator" class="alert warn">
    <AppIcon name="alert" :size="15" />
    read-only session — commands and run control are disabled.
  </div>

  <div class="layout">
    <div class="card">
      <div class="card-title">
        Command
        <span class="spacer" />
        <button class="btn ghost sm" @click="resetDefaults">reset defaults</button>
      </div>
      <div class="card-pad col">
        <select
          class="field cmd"
          :value="selected"
          @change="loadSchema(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="c in commands" :key="c" :value="c">{{ c }}</option>
        </select>
        <p v-if="group?.description" class="dim small desc">{{ group.description }}</p>

        <SchemaFormView
          v-if="group"
          :group="group"
          :state="formState"
          :disabled="!isOperator"
        />
        <div v-else class="empty">no plugin commands registered</div>

        <button
          class="btn primary start"
          :disabled="!isOperator || !selected || busy"
          @click="submit"
        >
          <AppIcon name="play" :size="15" />
          {{ busy ? "submitting…" : "Start run" }}
        </button>
      </div>
    </div>

    <div class="col right">
      <div class="card">
        <div class="card-title">Active run</div>
        <div class="card-pad col">
          <div v-if="!session.activeTaskId" class="empty">
            nothing submitted from this window yet
          </div>
          <template v-else>
            <div class="row">
              <span class="mono" :title="session.activeTaskId">{{ shortId(session.activeTaskId, 30) }}</span>
              <span class="spacer" />
              <StatusChip
                v-if="runState"
                :state="runState.state"
                :pulse="runState.state === 'running'"
              />
              <span v-else class="chip">submitted</span>
            </div>
            <div v-if="runState?.reason" class="dim small">{{ runState.reason }}</div>

            <template v-if="progress">
              <div class="progress"><div :style="{ width: percent + '%' }" /></div>
              <div class="dim small">
                step {{ progress.step }}<template v-if="progress.total"> / {{ progress.total }}</template>
              </div>
              <div v-if="progress.metrics && Object.keys(progress.metrics).length" class="metrics-grid">
                <div v-for="(v, k) in progress.metrics" :key="k" class="metric-cell">
                  <div class="mv">{{ fmtNum(v) }}</div>
                  <div class="mk">{{ k }}</div>
                </div>
              </div>
            </template>

            <div class="row controls">
              <button class="btn sm" :disabled="!isOperator || !canPause" @click="control('pause')">
                <AppIcon name="pause" :size="13" /> pause
              </button>
              <button class="btn sm" :disabled="!isOperator || !canResume" @click="control('resume')">
                <AppIcon name="play" :size="13" /> resume
              </button>
              <button class="btn sm danger" :disabled="!isOperator || !canCancel" @click="control('cancel')">
                <AppIcon name="stop" :size="13" /> cancel
              </button>
            </div>
          </template>

          <div v-if="session.lastAck" class="ack mono">{{ session.lastAck }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">Live preview</div>
        <div class="card-pad">
          <PreviewCanvas :preview="events.lastPreview?.preview ?? null" :height="210" />
        </div>
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: 14px;
  align-items: start;
}
@media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
.col { display: flex; flex-direction: column; gap: 12px; }
.right { gap: 14px; }
.cmd { font-weight: 600; }
.desc { margin-top: -4px; }
.start { align-self: flex-start; margin-top: 6px; }
.row { display: flex; align-items: center; gap: 8px; }
.controls { margin-top: 4px; }
.metrics-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  gap: 8px; margin-top: 2px;
}
.metric-cell {
  background: var(--card-2); border: 1px solid var(--border);
  border-radius: 10px; padding: 8px 10px;
}
.mv { font-size: 15px; font-weight: 700; font-family: var(--mono); }
.mk { font-size: 10.5px; color: var(--dim); margin-top: 1px; }
.ack {
  font-size: 11px; color: var(--dim);
  background: var(--card-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 9px; word-break: break-all;
}
</style>
