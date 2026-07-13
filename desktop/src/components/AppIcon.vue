<script setup lang="ts">
const props = withDefaults(defineProps<{ name: string; size?: number }>(), {
  size: 18,
});

/* Hand-drawn 24×24 stroke icon set (original artwork). */
const ICONS: Record<string, string> = {
  dashboard:
    "M20.2 15.5a8.5 8.5 0 1 0-16.4 0M12 13.5l3.5-3.5M12 13.5m-1.6 0a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0",
  devices:
    "M8 7h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zM10.5 10.5h3v3h-3zM9 4v3M15 4v3M9 17v3M15 17v3M4 9h3M4 15h3M17 9h3M17 15h3",
  play_circle:
    "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M10 8.7v6.6l5.4-3.3z",
  layers:
    "M12 3.5l8.5 4.7L12 12.9 3.5 8.2zM4 12.4l8 4.5 8-4.5M4 16.2l8 4.5 8-4.5",
  puzzle:
    "M10 5V4.2a1.8 1.8 0 1 1 3.6 0V5H17a1 1 0 0 1 1 1v3.4h.8a1.8 1.8 0 1 1 0 3.6H18V17a1 1 0 0 1-1 1h-3.4v.8a1.8 1.8 0 1 1-3.6 0V18H7a1 1 0 0 1-1-1v-3.4h-.8a1.8 1.8 0 1 1 0-3.6H6V6a1 1 0 0 1 1-1h3z",
  terminal:
    "M4 5.5h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1zM7 9.5l3 2.7-3 2.7M13 15h4",
  settings:
    "M4 7h16M4 12h16M4 17h16M9 7m-1.7 0a1.7 1.7 0 1 0 3.4 0a1.7 1.7 0 1 0-3.4 0M15 12m-1.7 0a1.7 1.7 0 1 0 3.4 0a1.7 1.7 0 1 0-3.4 0M7.5 17m-1.7 0a1.7 1.7 0 1 0 3.4 0a1.7 1.7 0 1 0-3.4 0",
  sun:
    "M12 8.5a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4L19 19M19 5l-1.6 1.6M6.6 17.4L5 19",
  moon:
    "M20.6 13.2A8.4 8.4 0 1 1 10.8 3.4a6.8 6.8 0 0 0 9.8 9.8z",
  zap: "M13 2.5L4.8 13.4h6l-1.5 8.1L17.6 10.6h-6z",
  refresh: "M20.4 12a8.4 8.4 0 1 1-2.5-6M20.4 3.6v4.4h-4.4",
  pause: "M9 6.5v11M15 6.5v11",
  play: "M8.5 5.8v12.4L18.6 12z",
  stop: "M7.5 7.5h9v9h-9z",
  x: "M6.5 6.5l11 11M17.5 6.5l-11 11",
  check: "M5 12.8l4.3 4.2L19 7.5",
  alert: "M12 3.6L2.8 19.4h18.4zM12 9.8v4.4M12 16.9v.3",
  power: "M12 3.5v7M16.9 6.6a7.2 7.2 0 1 1-9.8 0",
  wave: "M3 12.5h2.6l2.4-6 3.6 11 2.4-8 1.6 3h5.4",
  grid: "M4.5 4.5h6v6h-6zM13.5 4.5h6v6h-6zM4.5 13.5h6v6h-6zM13.5 13.5h6v6h-6z",
  scope:
    "M3.5 5.5h17v13h-17zM6 12c1.5-3.5 3-3.5 4.5 0s3 3.5 4.5 0M17.5 12H18",
  bars: "M5 17V11.5M9.5 17V7M14 17v-7.5M18.5 17V9",
  square_wave: "M3.5 15.5h3v-7h4.5v7H15.5v-7h5",
  book: "M6.5 4.5H17a2 2 0 0 1 2 2V19H8.5a2 2 0 0 1-2-2zM19 15.5H8.5a2 2 0 0 0-2 2",
  clock: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 8v4.2l2.8 1.6",
  info: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 11v5M12 8v.2",
};
</script>

<template>
  <svg
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="ICONS[props.name] ?? ICONS.info" />
  </svg>
</template>
