// Formatting helpers (Indian number formatting, signed values, time).
//
// All time formatters render in Asia/Kolkata (IST) by default.  The
// previous version used `toLocaleString` without a `timeZone` option,
// which silently rendered in the *host browser's* local timezone —
// a US user would see a 13:30 IST quote as "00:00" and conclude the
// data was 13 h stale when in fact it was live.

const IST = "Asia/Kolkata";

/**
 * Returns true when `v` is a finite number suitable for numeric formatting.
 * Rejects undefined, null, NaN, Infinity, -Infinity, and non-numbers.
 * A valid zero (0) is accepted as a legitimate value.
 */
export function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Safe numeric formatting — never throws on undefined/null/NaN/Infinity. */
export function fmtNum(v: number | null | undefined, opts?: Intl.NumberFormatOptions): string {
  if (!isFiniteNumber(v)) return "—";
  return v.toLocaleString("en-IN", opts);
}

/** Price with 2 decimals and thousands separators. */
export function fmtPrice(v: number | null | undefined): string {
  return fmtNum(v, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Signed price delta, e.g. +184.20 / -12.40 */
export function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(v)) return "—";
  const s = fmtNum(Math.abs(v), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return v >= 0 ? `+${s}` : `-${s}`;
}

/** Percent with sign, e.g. +0.75% */
export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(v)) return "—";
  const s = Math.abs(v).toFixed(digits);
  return `${v >= 0 ? "+" : "-"}${s}%`;
}

/** Compact volume, e.g. 1.2Cr / 84.3L (Indian crore/lakh style). */
export function fmtVolume(v: number | null | undefined): string {
  if (!isFiniteNumber(v)) return "—";
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)} L`;
  return fmtNum(Math.round(v));
}

/** epoch ms -> HH:MM:SS in IST (Asia/Kolkata). */
export function fmtTime(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: IST,
  });
}

/** alias of fmtTime — explicit "render in IST" naming. */
export const fmtTimeIST = fmtTime;

/** epoch ms -> "5s" / "2m" / "3h" — renders a fixed duration, not relative to now. */
export function fmtDuration(ms: number | null | undefined): string {
  if (!isFiniteNumber(ms)) return "—";
  if (ms < 0) return "—";
  const sec = ms / 1000;
  if (sec < 60) {
    return fmtNum(sec, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "s";
  }
  const min = sec / 60;
  if (min < 60) {
    return fmtNum(min, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "m";
  }
  const hr = min / 60;
  return fmtNum(hr, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "h";
}

/** epoch ms -> "12s ago" / "5m ago" / "2h ago" in IST-aware now. */
export function fmtAgo(ms: number | null): string {
  if (ms == null) return "—";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/** epoch ms -> HH:MM in IST. */
export function fmtHM(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: IST,
  });
}

/** epoch ms -> "DD MMM HH:MM" in IST. */
export function fmtDateTime(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: IST,
  });
}
