<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { PreviewPayload } from "../api/contracts";

const props = withDefaults(
  defineProps<{ preview: PreviewPayload | null; height?: number }>(),
  { height: 220 },
);

const wrap = ref<HTMLDivElement | null>(null);
const canvas = ref<HTMLCanvasElement | null>(null);
let observer: ResizeObserver | null = null;

const label = computed(() => {
  const p = props.preview;
  if (!p) return "waiting for data…";
  if (p.preview_type === "image") return "image preview";
  if (p.preview_type === "waveform") return `waveform (s / ${p.y_unit ?? "V"})`;
  if (p.preview_type === "scalar_series") return `scalar series — ${p.name}`;
  return "spectrum (nm / dBm)";
});

function series(p: PreviewPayload): [number[], number[]] | null {
  if (p.preview_type === "waveform") return [p.t_s, p.y];
  if (p.preview_type === "scalar_series") return [p.x, p.y];
  if (p.preview_type === "image") return null;
  return [(p as { x_nm: number[] }).x_nm, (p as { y_dbm: number[] }).y_dbm];
}

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function draw(): void {
  const el = canvas.value;
  const box = wrap.value;
  if (!el || !box) return;
  const dpr = window.devicePixelRatio || 1;
  const w = box.clientWidth;
  const h = props.height;
  el.width = Math.max(1, Math.floor(w * dpr));
  el.height = Math.max(1, Math.floor(h * dpr));
  el.style.width = `${w}px`;
  el.style.height = `${h}px`;
  const ctx = el.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const p = props.preview;
  if (!p) {
    ctx.fillStyle = css("--dim");
    ctx.font = "12px " + css("--font");
    ctx.textAlign = "center";
    ctx.fillText("no preview yet — start a run", w / 2, h / 2);
    return;
  }
  if (p.preview_type === "image") {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(w / img.width, h / img.height);
      const dw = img.width * scale;
      const dh = img.height * scale;
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh);
    };
    img.src = "data:image/png;base64," + p.png_base64;
    return;
  }
  const xy = series(p);
  if (!xy || xy[0].length < 2) return;
  const [xs, ys] = xy;
  const pad = { l: 10, r: 10, t: 10, b: 10 };
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const px = (x: number) =>
    pad.l + (w - pad.l - pad.r) * (xMax > xMin ? (x - xMin) / (xMax - xMin) : 0.5);
  const py = (y: number) =>
    h - pad.b - (h - pad.t - pad.b) * (yMax > yMin ? (y - yMin) / (yMax - yMin) : 0.5);

  // faint grid
  ctx.strokeStyle = css("--border");
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const gy = pad.t + ((h - pad.t - pad.b) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, gy);
    ctx.lineTo(w - pad.r, gy);
    ctx.stroke();
  }

  // gradient trace
  const grad = ctx.createLinearGradient(0, 0, w, 0);
  grad.addColorStop(0, "#8b5cf6");
  grad.addColorStop(1, "#22d3ee");
  ctx.strokeStyle = grad;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), py(ys[0]));
  for (let i = 1; i < xs.length; i++) ctx.lineTo(px(xs[i]), py(ys[i]));
  ctx.stroke();

  ctx.fillStyle = css("--dim");
  ctx.font = "10.5px " + css("--mono");
  ctx.textAlign = "left";
  ctx.fillText(`${yMin.toPrecision(4)} … ${yMax.toPrecision(4)}`, pad.l + 2, pad.t + 4);
}

watch(() => props.preview, draw);
onMounted(() => {
  observer = new ResizeObserver(draw);
  if (wrap.value) observer.observe(wrap.value);
  draw();
});
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
  <div ref="wrap" class="preview-wrap">
    <canvas ref="canvas" />
    <div class="preview-label">{{ label }}</div>
  </div>
</template>

<style scoped>
.preview-wrap { position: relative; width: 100%; }
.preview-label {
  position: absolute; right: 10px; top: 8px;
  font-size: 11px; color: var(--dim); font-family: var(--mono);
  pointer-events: none;
}
</style>
