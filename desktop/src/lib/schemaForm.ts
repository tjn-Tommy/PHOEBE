/* Schema-driven form model — a faithful port of phoebe/ui/form_model.py
 * (plan §6.6 / PR D-2, the H12 drift fix).
 *
 * The plugin config's JSON Schema is the ONLY source of defaults, ranges,
 * enums and descriptions; this module derives a renderable tree from it and
 * `defaultsPayload` must equal the pydantic model's own defaults. */

export type FieldKind = "int" | "float" | "bool" | "str" | "enum" | "json";

export interface FormField {
  node: "field";
  name: string;
  kind: FieldKind;
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  exclusiveMin: boolean;
  exclusiveMax: boolean;
  choices: unknown[];
  description: string;
}

export interface FormGroup {
  node: "group";
  name: string;
  items: (FormField | FormGroup)[];
  description: string;
}

type Schema = Record<string, unknown>;

function deref(node: Schema, defs: Record<string, Schema>): Schema {
  const ref = node["$ref"];
  if (typeof ref === "string") {
    const resolved = defs[ref.split("/").pop() ?? ""] ?? {};
    const local = Object.fromEntries(
      Object.entries(node).filter(([k]) => k !== "$ref"),
    );
    return { ...resolved, ...local };
  }
  return node;
}

function scalarField(name: string, prop: Schema, dflt: unknown): FormField {
  const description = typeof prop.description === "string" ? prop.description : "";
  const base: FormField = {
    node: "field", name, kind: "json", default: dflt,
    minimum: null, maximum: null, exclusiveMin: false, exclusiveMax: false,
    choices: [], description,
  };
  if (Array.isArray(prop.enum)) {
    return { ...base, kind: "enum", choices: [...(prop.enum as unknown[])] };
  }
  const kindOfType: Record<string, FieldKind> = {
    integer: "int", number: "float", boolean: "bool", string: "str",
  };
  const kind = kindOfType[String(prop.type ?? "")];
  if (kind === undefined) return base;
  const field: FormField = { ...base, kind };
  if (typeof prop.minimum === "number") field.minimum = prop.minimum;
  if (typeof prop.exclusiveMinimum === "number") {
    field.minimum = prop.exclusiveMinimum;
    field.exclusiveMin = true;
  }
  if (typeof prop.maximum === "number") field.maximum = prop.maximum;
  if (typeof prop.exclusiveMaximum === "number") {
    field.maximum = prop.exclusiveMaximum;
    field.exclusiveMax = true;
  }
  return field;
}

function buildItem(
  name: string, raw: Schema, defs: Record<string, Schema>,
): FormField | FormGroup {
  let prop = deref(raw, defs);
  let dflt = prop.default;
  if (Array.isArray(prop.anyOf)) {          // Optional[X] → the non-null member
    const inner = (prop.anyOf as Schema[])
      .map((p) => deref(p, defs))
      .filter((p) => p.type !== "null");
    if (inner.length === 1) {
      prop = { ...inner[0], description: prop.description ?? "" };
      if (dflt !== null && dflt !== undefined) prop.default = dflt;
      else if (!("default" in prop)) prop.default = dflt;
      dflt = prop.default;
    }
  }
  if (prop.type === "object" && "properties" in prop) {
    const overrides =
      dflt !== null && typeof dflt === "object" && !Array.isArray(dflt)
        ? (dflt as Record<string, unknown>)
        : {};
    return buildGroup(name, prop, defs, overrides);
  }
  return scalarField(name, prop, dflt ?? null);
}

function applyOverride(
  item: FormField | FormGroup, value: unknown,
): FormField | FormGroup {
  if (item.node === "field") return { ...item, default: value };
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const v = value as Record<string, unknown>;
    return {
      ...item,
      items: item.items.map((i) => (i.name in v ? applyOverride(i, v[i.name]) : i)),
    };
  }
  return item;
}

function buildGroup(
  name: string, node: Schema, defs: Record<string, Schema>,
  overrides: Record<string, unknown> | null = null,
): FormGroup {
  const items: (FormField | FormGroup)[] = [];
  const props = (node.properties ?? {}) as Record<string, Schema>;
  for (const [propName, raw] of Object.entries(props)) {
    let item = buildItem(propName, raw, defs);
    if (overrides && propName in overrides) {
      item = applyOverride(item, overrides[propName]);
    }
    items.push(item);
  }
  return {
    node: "group", name, items,
    description: typeof node.description === "string" ? node.description : "",
  };
}

/** JSON Schema (`model_json_schema()`) → renderable form tree. */
export function buildFormModel(schema: Schema): FormGroup {
  return buildGroup("", schema, (schema["$defs"] ?? {}) as Record<string, Schema>);
}

/** The payload an untouched form submits — equals the contract's defaults. */
export function defaultsPayload(group: FormGroup): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const item of group.items) {
    payload[item.name] =
      item.node === "group" ? defaultsPayload(item) : structuredClone(item.default);
  }
  return payload;
}
