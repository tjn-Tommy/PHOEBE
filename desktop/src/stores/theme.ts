import { reactive, watchEffect } from "vue";

export type ThemeMode = "light" | "dark" | "system";

const media = window.matchMedia("(prefers-color-scheme: dark)");

export const theme = reactive({
  mode: (localStorage.getItem("phoebe.theme") as ThemeMode) || "system",
  systemDark: media.matches,
});

media.addEventListener("change", (e) => (theme.systemDark = e.matches));

export function resolvedTheme(): "light" | "dark" {
  if (theme.mode === "system") return theme.systemDark ? "dark" : "light";
  return theme.mode;
}

export function setTheme(mode: ThemeMode): void {
  theme.mode = mode;
  localStorage.setItem("phoebe.theme", mode);
}

watchEffect(() => {
  document.documentElement.dataset.theme = resolvedTheme();
});
