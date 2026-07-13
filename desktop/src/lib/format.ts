/* Small presentation helpers shared by pages. */

export function shortId(id: string, n = 20): string {
  return id.length <= n ? id : id.slice(0, n) + "…";
}

export function fmtNum(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  if (Number.isInteger(v) && Math.abs(v) < 1e7) return String(v);
  return v.toPrecision(4);
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Map run/device/plugin states onto chip tones. */
export function toneOf(state: string): string {
  switch (state) {
    case "completed": case "ok": case "ready": case "loaded":
      return "ok";
    case "running": case "preparing": case "finalizing": case "connecting":
      return "info";
    case "paused": case "pausing": case "queued": case "degraded":
    case "outdated": case "interrupted":
      return "warn";
    case "failed": case "aborted": case "error": case "offline":
    case "refused": case "stopping": case "operator_review_required":
      return "bad";
    case "disabled":
      return "";
    default:
      return "";
  }
}
