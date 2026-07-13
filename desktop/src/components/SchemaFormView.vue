<script setup lang="ts">
import type { FormField, FormGroup } from "../lib/schemaForm";
import SchemaField from "./SchemaField.vue";

const props = defineProps<{
  group: FormGroup;
  state: Record<string, unknown>;
  disabled?: boolean;
}>();

function childState(name: string): Record<string, unknown> {
  return props.state[name] as Record<string, unknown>;
}

function rangeHint(f: FormField): string {
  if (f.minimum === null && f.maximum === null) return "";
  const lo = f.minimum === null ? "−∞" : `${f.exclusiveMin ? ">" : "≥"} ${f.minimum}`;
  const hi = f.maximum === null ? "∞" : `${f.exclusiveMax ? "<" : "≤"} ${f.maximum}`;
  return `${lo} · ${hi}`;
}
</script>

<template>
  <div class="schema-form">
    <template v-for="item in props.group.items" :key="item.name">
      <fieldset v-if="item.node === 'group'" class="sub-group">
        <legend>{{ item.name }}</legend>
        <SchemaFormView
          :group="item"
          :state="childState(item.name)"
          :disabled="props.disabled"
        />
      </fieldset>
      <div v-else class="form-row">
        <label class="field-label">
          {{ item.name }}
          <span v-if="rangeHint(item)" class="range">{{ rangeHint(item) }}</span>
        </label>
        <SchemaField :field="item" :state="props.state" :disabled="props.disabled" />
        <div v-if="item.description" class="hint">{{ item.description }}</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.schema-form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px 16px;
}
.sub-group {
  grid-column: 1 / -1;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 14px 14px;
  margin: 2px 0 0;
  background: var(--card-2);
}
.sub-group > legend {
  padding: 0 6px;
  font-size: 11.5px;
  font-weight: 650;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: .05em;
}
.form-row { min-width: 0; }
.range {
  float: right;
  font-weight: 500;
  font-size: 10.5px;
  color: var(--dim);
  opacity: .8;
  font-family: var(--mono);
}
.hint { margin-top: 3px; font-size: 11px; color: var(--dim); }
</style>
