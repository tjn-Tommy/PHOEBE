<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { FormField } from "../lib/schemaForm";

const props = defineProps<{
  field: FormField;
  state: Record<string, unknown>;
  disabled?: boolean;
}>();

const numStep = computed(() => (props.field.kind === "int" ? 1 : "any"));
const numMin = computed(() => {
  const f = props.field;
  if (f.minimum === null) return undefined;
  return f.exclusiveMin ? f.minimum + (f.kind === "int" ? 1 : 1e-6) : f.minimum;
});
const numMax = computed(() => {
  const f = props.field;
  if (f.maximum === null) return undefined;
  return f.exclusiveMax ? f.maximum - (f.kind === "int" ? 1 : 1e-6) : f.maximum;
});

function onNumber(e: Event): void {
  const v = (e.target as HTMLInputElement).valueAsNumber;
  if (Number.isNaN(v)) return;
  props.state[props.field.name] = props.field.kind === "int" ? Math.round(v) : v;
}

function onText(e: Event): void {
  props.state[props.field.name] = (e.target as HTMLInputElement).value;
}

function onBool(e: Event): void {
  props.state[props.field.name] = (e.target as HTMLInputElement).checked;
}

/* enum: options are arbitrary JSON values — select by index */
const enumIdx = computed({
  get: () => {
    const cur = JSON.stringify(props.state[props.field.name]);
    const i = props.field.choices.findIndex((c) => JSON.stringify(c) === cur);
    return i >= 0 ? i : 0;
  },
  set: (i: number) => {
    props.state[props.field.name] = props.field.choices[i];
  },
});

/* json: free-form editor kept in sync with the typed state */
const jsonText = ref(JSON.stringify(props.state[props.field.name] ?? null, null, 1));
const jsonBad = ref(false);

function onJson(e: Event): void {
  jsonText.value = (e.target as HTMLTextAreaElement).value;
  try {
    props.state[props.field.name] = JSON.parse(jsonText.value);
    jsonBad.value = false;
  } catch {
    jsonBad.value = true;
  }
}

watch(
  () => props.state[props.field.name],
  (v) => {
    if (props.field.kind !== "json") return;
    try {
      if (JSON.stringify(JSON.parse(jsonText.value)) === JSON.stringify(v)) return;
    } catch {
      /* editor holds invalid text — replace it */
    }
    jsonText.value = JSON.stringify(v ?? null, null, 1);
    jsonBad.value = false;
  },
);
</script>

<template>
  <select
    v-if="field.kind === 'enum'"
    v-model="enumIdx"
    class="field"
    :disabled="disabled"
  >
    <option v-for="(c, i) in field.choices" :key="i" :value="i">
      {{ String(c) }}
    </option>
  </select>

  <label v-else-if="field.kind === 'bool'" class="switch">
    <input
      type="checkbox"
      :checked="Boolean(state[field.name])"
      :disabled="disabled"
      @change="onBool"
    />
    <span class="track" />
  </label>

  <input
    v-else-if="field.kind === 'int' || field.kind === 'float'"
    type="number"
    class="field"
    :value="state[field.name] as number"
    :min="numMin"
    :max="numMax"
    :step="numStep"
    :disabled="disabled"
    @input="onNumber"
  />

  <input
    v-else-if="field.kind === 'str'"
    type="text"
    class="field"
    :value="state[field.name] as string"
    :disabled="disabled"
    @input="onText"
  />

  <textarea
    v-else
    class="field"
    :class="{ invalid: jsonBad }"
    :value="jsonText"
    rows="3"
    spellcheck="false"
    :disabled="disabled"
    @input="onJson"
  />
</template>
