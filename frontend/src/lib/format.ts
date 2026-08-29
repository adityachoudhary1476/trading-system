// Formatting helpers (Indian number formatting, signed values, time).

export function fmtNum(v: number, opts?: Intl.NumberFormatOptions): string {
  return v.toLocaleString("en-IN", opts);
}

/** Price with 2 decimals and thousands separators. */
export function fmtPrice(v: number): string {
  return fmtNum(v, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Signed price delta, e.g. +184.20 / -12.40 */
export function fmtSigned(v: number, digits = 2): string {
  const s = fmtNum(Math.abs(v), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return v >= 0 ? `+${s}` : `-${s}`;
}

/** Percent with sign, e.g. +0.75% */
export function fmtPct(v: number, digits = 2): string {
  const s = Math.abs(v).toFixed(digits);
  return `${v >= 0 ? "+" : "-"}${s}%`;
}

/** Compact volume, e.g. 1.2Cr / 84.3L (Indian crore/lakh style). */
export function fmtVolume(v: number): string {
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)} L`;
  return fmtNum(Math.round(v));
}

/** epoch ms -> HH:MM:SS */
export function fmtTime(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** epoch ms -> "12s ago" / "5m ago" / "2h ago" */
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

/** epoch ms -> HH:MM */
export function fmtHM(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}

/** epoch ms -> DD MMM HH:MM */
export function fmtDateTime(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
