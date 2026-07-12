/* PHOEBE static client (plan E-3, zero-build variant).
 *
 * Everything here talks to /api/v1 only: envelope in, typed codes out —
 * no prose is ever parsed.  Forms are generated from the plugin's own
 * JSON Schema (single source of defaults, plan §6.6).  The event stream is
 * fetch-based SSE so the session token rides the Authorization header
 * (native EventSource cannot set headers), with since_seq gap repair.
 */
"use strict";

const API = "/api/v1";
const TOPICS = ["progress", "run_state", "data_pointer", "device_health", "error", "log"];
const PINNED_CONTRACTS_VERSION = 2;   // must match the version file (A14)

let token = sessionStorage.getItem("phoebe_token") || "";
let lastSeq = 0;
let activeTask = null;
let streaming = false;

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ api */

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      authorization: "Bearer " + token,
      ...(opts.body ? { "content-type": "application/json" } : {}),
      ...(opts.headers || {}),
    },
  });
  let body;
  try { body = await res.json(); } catch { body = { status: "error", error: { code: "internal", message: "bad response" } }; }
  if (body.status === "error") {
    if (body.error && body.error.code === "unauthorized") showTokenGate("invalid or expired token");
    throw Object.assign(new Error(body.error ? body.error.message : "request failed"),
                        { apiError: body.error });
  }
  return body;
}

/* ----------------------------------------------------------- token gate */

function showTokenGate(message) {
  streaming = false;
  $("token-error").textContent = message || "";
  $("token-gate").classList.remove("hidden");
  $("token-input").focus();
}

$("token-connect").addEventListener("click", async () => {
  token = $("token-input").value.trim();
  sessionStorage.setItem("phoebe_token", token);
  try {
    await connect();
    $("token-gate").classList.add("hidden");
  } catch (e) {
    $("token-error").textContent = (e.apiError && e.apiError.message) || String(e);
  }
});
$("token-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("token-connect").click();
});

/* ----------------------------------------------------------------- meta */

async function connect() {
  const meta = (await api("/meta")).data;
  $("meta-info").textContent =
    `v${meta.app_version} · api v${meta.api_version} · contracts v${meta.contracts_version} · ${meta.role}`;
  if (meta.contracts_version !== PINNED_CONTRACTS_VERSION || meta.static_ui === "outdated") {
    const b = $("banner");
    b.textContent = `contracts version skew: UI pins v${PINNED_CONTRACTS_VERSION}, backend speaks v${meta.contracts_version} — rebuild the static client`;
    b.classList.remove("hidden");
  }
  if (meta.role !== "operator") {
    for (const id of ["submit-btn", "pause-btn", "resume-btn", "cancel-btn"]) $(id).disabled = true;
  }
  await Promise.all([refreshDevices(), refreshRuns(), loadCommands()]);
  if (!streaming) { streaming = true; streamLoop(); }
}

/* -------------------------------------------------------------- devices */

async function refreshDevices() {
  const rows = (await api("/devices")).data;
  const tbody = $("devices").querySelector("tbody");
  tbody.innerHTML = "";
  for (const d of rows) {
    const tr = document.createElement("tr");
    tr.dataset.iid = d.instrument_id;
    tr.innerHTML = `<td>${d.instrument_id}</td><td>${d.kind}</td>` +
                   `<td>${d.backend}</td><td class="lifecycle">${d.lifecycle}</td>`;
    tbody.appendChild(tr);
  }
}

function updateDeviceHealth(ev) {
  const tr = $("devices").querySelector(`tr[data-iid="${ev.instrument_id}"]`);
  if (tr) tr.querySelector(".lifecycle").textContent =
    ev.status === "ok" ? "ready" : ev.status;
}

/* ----------------------------------------------------- schema-driven form */

let currentSchema = null;

async function loadCommands() {
  const commands = (await api("/plugins/commands")).data;
  const select = $("command-select");
  select.innerHTML = "";
  for (const c of commands) {
    const opt = document.createElement("option");
    opt.value = c; opt.textContent = c;
    select.appendChild(opt);
  }
  if (commands.length) await loadSchema(commands[0]);
}

$("command-select").addEventListener("change", (e) => loadSchema(e.target.value));

function deref(node, defs) {
  if (node && node.$ref) {
    const name = node.$ref.split("/").pop();
    return defs[name] || node;
  }
  return node;
}

async function loadSchema(command) {
  currentSchema = (await api(`/plugins/commands/${encodeURIComponent(command)}/schema`)).data;
  const defs = currentSchema.$defs || {};
  const form = $("command-form");
  form.innerHTML = "";
  for (const [name, rawProp] of Object.entries(currentSchema.properties || {})) {
    const prop = deref(rawProp, defs);
    const label = document.createElement("label");
    label.textContent = name + (prop.description ? ` — ${prop.description}` : "");
    form.appendChild(label);
    form.appendChild(fieldFor(name, prop, defs));
  }
}

function fieldFor(name, prop, defs) {
  // anyOf [X, null] → optional X
  if (prop.anyOf) {
    const inner = prop.anyOf.map((p) => deref(p, defs)).find((p) => p.type !== "null");
    if (inner) prop = { ...inner, default: prop.default };
  }
  let el;
  if (prop.enum) {
    el = document.createElement("select");
    for (const v of prop.enum) {
      const opt = document.createElement("option");
      opt.value = JSON.stringify(v); opt.textContent = String(v);
      opt.selected = v === prop.default;
      el.appendChild(opt);
    }
    el.dataset.kind = "enum";
  } else if (prop.type === "boolean") {
    el = document.createElement("input");
    el.type = "checkbox"; el.checked = Boolean(prop.default);
    el.dataset.kind = "boolean";
  } else if (prop.type === "integer" || prop.type === "number") {
    el = document.createElement("input");
    el.type = "number";
    if (prop.type === "number") el.step = "any";
    for (const [k, attr] of [["minimum", "min"], ["maximum", "max"],
                             ["exclusiveMinimum", "min"], ["exclusiveMaximum", "max"]])
      if (prop[k] !== undefined) el[attr] = prop[k];
    if (prop.default !== undefined) el.value = prop.default;
    el.dataset.kind = prop.type;
  } else if (prop.type === "string") {
    el = document.createElement("input");
    el.type = "text";
    if (prop.default !== undefined) el.value = prop.default;
    el.dataset.kind = "string";
  } else {
    // nested object/array: JSON textarea prefilled with schema defaults
    el = document.createElement("textarea");
    el.value = JSON.stringify(defaultsOf(prop, defs), null, 1);
    el.dataset.kind = "json";
  }
  el.name = name;
  return el;
}

function defaultsOf(prop, defs) {
  prop = deref(prop, defs);
  if (prop.default !== undefined) return prop.default;
  if (prop.type === "object" && prop.properties) {
    const out = {};
    for (const [k, v] of Object.entries(prop.properties)) out[k] = defaultsOf(v, defs);
    return out;
  }
  if (prop.type === "array") return [];
  return null;
}

function collectPayload() {
  const payload = {};
  for (const el of $("command-form").elements) {
    if (!el.name) continue;
    const kind = el.dataset.kind;
    if (kind === "boolean") payload[el.name] = el.checked;
    else if (kind === "integer") { if (el.value !== "") payload[el.name] = parseInt(el.value, 10); }
    else if (kind === "number") { if (el.value !== "") payload[el.name] = parseFloat(el.value); }
    else if (kind === "enum") payload[el.name] = JSON.parse(el.value);
    else if (kind === "json") { const v = JSON.parse(el.value); if (v !== null) payload[el.name] = v; }
    else if (el.value !== "") payload[el.name] = el.value;
  }
  return payload;
}

/* --------------------------------------------------------------- submit */

$("submit-btn").addEventListener("click", async () => {
  let payload;
  try { payload = collectPayload(); }
  catch (e) { $("ack-line").textContent = `form error: ${e}`; return; }
  const envelope = {
    command_id: crypto.randomUUID(),           // ledger: new id per attempt
    command: $("command-select").value,
    payload, issued_by: "web_ui",
  };
  try {
    const body = await api("/commands", { method: "POST", body: JSON.stringify(envelope) });
    const ack = body.data;
    $("ack-line").textContent = `ack: ${ack.code}` +
      (ack.task_id ? ` → ${ack.task_id}` : "") + (ack.reason ? ` (${ack.reason})` : "");
    if (ack.accepted && ack.task_id) setActiveTask(ack.task_id);
  } catch (e) {
    $("ack-line").textContent = (e.apiError && e.apiError.message) || String(e);
  }
});

/* ----------------------------------------------------------- run control */

function setActiveTask(taskId) {
  activeTask = taskId;
  $("run-line").textContent = `${taskId} — submitted`;
  $("progress-line").textContent = "";
  for (const id of ["pause-btn", "resume-btn", "cancel-btn"]) $(id).disabled = false;
}

async function runControl(action) {
  if (!activeTask) return;
  const body = await api(`/runs/${encodeURIComponent(activeTask)}/${action}`, { method: "POST" });
  const ack = body.data;
  $("ack-line").textContent = `${action}: ${ack.code}` + (ack.reason ? ` (${ack.reason})` : "");
}
$("pause-btn").addEventListener("click", () => runControl("pause"));
$("resume-btn").addEventListener("click", () => runControl("resume"));
$("cancel-btn").addEventListener("click", () => runControl("cancel"));

/* ------------------------------------------------------------------ runs */

async function refreshRuns() {
  const rows = (await api("/runs?limit=50")).data;
  const tbody = $("runs").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td title="${r.run_id}">${r.run_id.slice(0, 18)}</td><td>${r.command || ""}</td>` +
      `<td class="state-${r.state}">${r.state}</td><td>${r.finalized || ""}</td>`;
    tbody.appendChild(tr);
  }
}
$("runs-refresh").addEventListener("click", refreshRuns);

/* ---------------------------------------------------------------- stream */

async function streamLoop() {
  while (streaming) {
    try {
      const params = new URLSearchParams({ topics: TOPICS.join(",") });
      if (lastSeq > 0) params.set("since_seq", String(lastSeq));
      const res = await fetch(`${API}/events/stream?${params}`, {
        headers: { authorization: "Bearer " + token },
      });
      if (res.status === 401) { showTokenGate("invalid or expired token"); return; }
      $("conn-dot").className = "dot on";
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          handleFrame(buf.slice(0, cut));
          buf = buf.slice(cut + 2);
        }
      }
    } catch (e) { /* connection dropped — retry below */ }
    $("conn-dot").className = "dot off";
    if (streaming) await sleep(3000);   // since_seq repairs the gap on reconnect
  }
}

function handleFrame(frame) {
  let id = null, type = null, data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) id = parseInt(line.slice(3).trim(), 10);
    else if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (Number.isInteger(id) && id > 0) lastSeq = id;
  if (!type || !data || type === "stream_reset") return;
  let ev;
  try { ev = JSON.parse(data); } catch { return; }
  dispatch(type, ev);
}

function dispatch(type, ev) {
  if (type === "run_state") {
    if (ev.task_id === activeTask) {
      $("run-line").innerHTML =
        `${ev.task_id} — <span class="state-${ev.state}">${ev.state}</span>` +
        (ev.reason ? ` (${ev.reason})` : "") + (ev.final ? " · final" : "");
      if (ev.final) {
        for (const id of ["pause-btn", "resume-btn", "cancel-btn"]) $(id).disabled = true;
      }
    }
    if (ev.final) refreshRuns();        // catalog row is now stable
  } else if (type === "progress") {
    if (ev.task_id === activeTask) {
      const metrics = Object.entries(ev.metrics || {})
        .map(([k, v]) => `${k}=${Number(v).toPrecision(4)}`).join(" ");
      $("progress-line").textContent =
        `step ${ev.step}${ev.total ? "/" + ev.total : ""} ${metrics}`;
    }
  } else if (type === "data_pointer") {
    if (ev.preview) drawPreview(ev.preview);
  } else if (type === "device_health") {
    updateDeviceHealth(ev);
  } else if (type === "log" || type === "error") {
    appendLog(type === "error" ? "error" : ev.level || "info",
              type === "error" ? `[${ev.code}] ${ev.message}` : ev.message);
  }
}

function appendLog(level, message) {
  const log = $("log");
  const line = document.createElement("div");
  line.className = level;
  line.textContent = message;
  log.appendChild(line);
  while (log.childElementCount > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

/* --------------------------------------------------------------- preview */

function drawPreview(preview) {
  const canvas = $("preview");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (preview.preview_type === "image") {
    $("preview-title").textContent = "Live preview — image";
    const img = new Image();
    img.onload = () => ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    img.src = "data:image/png;base64," + preview.png_base64;
    return;
  }
  const [xs, ys, label] =
    preview.preview_type === "spectrum" ? [preview.x_nm, preview.y_dbm, "spectrum (nm / dBm)"] :
    preview.preview_type === "waveform" ? [preview.t_s, preview.y, `waveform (s / ${preview.y_unit})`] :
    [preview.x, preview.y, `scalar series: ${preview.name}`];
  $("preview-title").textContent = "Live preview — " + label;
  if (!xs || !ys || xs.length < 2) return;
  const [xMin, xMax] = [Math.min(...xs), Math.max(...xs)];
  const [yMin, yMax] = [Math.min(...ys), Math.max(...ys)];
  const px = (x) => 6 + (canvas.width - 12) * (xMax > xMin ? (x - xMin) / (xMax - xMin) : 0.5);
  const py = (y) => canvas.height - 6 -
    (canvas.height - 12) * (yMax > yMin ? (y - yMin) / (yMax - yMin) : 0.5);
  ctx.strokeStyle = "#4da3ff";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), py(ys[0]));
  for (let i = 1; i < xs.length; i++) ctx.lineTo(px(xs[i]), py(ys[i]));
  ctx.stroke();
  ctx.fillStyle = "#8b93a1";
  ctx.font = "11px monospace";
  ctx.fillText(`${yMin.toPrecision(4)} … ${yMax.toPrecision(4)}`, 8, 14);
}

/* ------------------------------------------------------------------ init */

(async () => {
  if (!token) { showTokenGate(); return; }
  try { await connect(); } catch { showTokenGate(); }
})();
