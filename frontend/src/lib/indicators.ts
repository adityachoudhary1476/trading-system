// Pure technical-analysis indicators computed from OHLCVBar[].
// All deterministic from the bar array — works for both mock and real data
// without any backend change. (lightweight-charts overlay/separate-scale series
// are added by the chart component.)

export type LinePoint = { time: number; value: number };

function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    out.push(i >= period - 1 ? sum / period : null);
  }
  return out;
}

export function ema(values: number[], period: number): (number | null)[] {
  const k = 2 / (period + 1);
  let prev: number | null = null;
  return values.map((v) => {
    prev = prev === null ? v : v * k + prev * (1 - k);
    return prev;
  });
}

function toLine(bars: { time: number }[], series: (number | null)[]): LinePoint[] {
  const out: LinePoint[] = [];
  for (let i = 0; i < bars.length; i++) {
    const v = series[i];
    if (v != null && Number.isFinite(v)) out.push({ time: bars[i].time / 1000, value: v });
  }
  return out;
}

export const smaLine = (bars: { time: number; close: number }[], period: number) =>
  toLine(bars, sma(bars.map((b) => b.close), period));

export const emaLine = (bars: { time: number; close: number }[], period: number) =>
  toLine(bars, ema(bars.map((b) => b.close), period));

export function bollingerBands(bars: { time: number; close: number }[], period = 20, mult = 2) {
  const closes = bars.map((b) => b.close);
  const mid = sma(closes, period);
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) {
      upper.push(null);
      lower.push(null);
      continue;
    }
    const window = closes.slice(i - period + 1, i + 1);
    const mean = mid[i] as number;
    const variance = window.reduce((a, c) => a + (c - mean) ** 2, 0) / period;
    const sd = Math.sqrt(variance);
    upper.push(mean + mult * sd);
    lower.push(mean - mult * sd);
  }
  return {
    upper: toLine(bars, upper),
    middle: toLine(bars, mid),
    lower: toLine(bars, lower),
  };
}

export function vwap(bars: { time: number; high: number; low: number; close: number; volume: number }[]): LinePoint[] {
  let cumPV = 0;
  let cumV = 0;
  const out: LinePoint[] = [];
  for (const b of bars) {
    const typical = (b.high + b.low + b.close) / 3;
    cumPV += typical * b.volume;
    cumV += b.volume;
    if (cumV > 0) out.push({ time: b.time / 1000, value: cumPV / cumV });
  }
  return out;
}

/** RSI (Wilder smoothing). Returns values 0..100, null until warm-up. */
export function rsi(bars: { time: number; close: number }[], period = 14): LinePoint[] {
  const closes = bars.map((b) => b.close);
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length <= period) return [];
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) gain += d;
    else loss -= d;
  }
  let avgG = gain / period;
  let avgL = loss / period;
  out[period] = 100 - 100 / (1 + (avgG / (avgL || 1e-9)));
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d > 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgG = (avgG * (period - 1) + g) / period;
    avgL = (avgL * (period - 1) + l) / period;
    out[i] = 100 - 100 / (1 + (avgG / (avgL || 1e-9)));
  }
  return toLine(bars, out);
}
