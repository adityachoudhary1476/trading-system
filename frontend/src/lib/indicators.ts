// Pure technical-analysis indicators computed from OHLCVBar[].
// All deterministic from the bar array — works for both mock and real data
// without any backend change. (lightweight-charts overlay/separate-scale series
// are added by the chart component.)

export type LinePoint = { time: number; value: number };

export function sma(values: number[], period: number): LinePoint[] {
  const out: LinePoint[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out.push({ time: i, value: sum / period });
  }
  return out;
}

export function ema(values: number[], period: number): LinePoint[] {
  const k = 2 / (period + 1);
  let prev: number | null = null;
  const out: LinePoint[] = [];
  values.forEach((v, i) => {
    prev = prev === null ? v : v * k + prev * (1 - k);
    out.push({ time: i, value: prev });
  });
  return out;
}

function toLine(bars: { time: number }[], series: LinePoint[]): LinePoint[] {
  const out: LinePoint[] = [];
  for (let i = 0; i < bars.length && i < series.length; i++) {
    const v = series[i];
    if (v && Number.isFinite(v.value)) out.push({ time: bars[i].time / 1000, value: v.value });
  }
  return out;
}

export const smaLine = (bars: { time: number; close: number }[], period: number) => {
  const closes = bars.map((b) => b.close);
  const smaPoints = sma(closes, period);
  return toLine(bars, smaPoints);
};

export const emaLine = (bars: { time: number; close: number }[], period: number) => {
  const closes = bars.map((b) => b.close);
  const emaPoints = ema(closes, period);
  return toLine(bars, emaPoints);
};

export function bollingerBands(bars: { time: number; close: number }[], period = 20, mult = 2) {
  const closes = bars.map((b) => b.close);
  const mid = sma(closes, period);
  const upper: LinePoint[] = [];
  const lower: LinePoint[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) continue;
    const window = closes.slice(i - period + 1, i + 1);
    const mean = mid[i - period + 1]?.value ?? 0;
    const variance = window.reduce((a, c) => a + (c - mean) ** 2, 0) / period;
    const sd = Math.sqrt(variance);
    upper.push({ time: bars[i].time / 1000, value: mean + mult * sd });
    lower.push({ time: bars[i].time / 1000, value: mean - mult * sd });
  }
  return {
    upper,
    middle: mid.slice(period - 1).map((p) => ({ time: p.time, value: p.value })),
    lower,
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
  const out: LinePoint[] = [];
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
  out.push({ time: bars[period].time / 1000, value: 100 - 100 / (1 + (avgG / (avgL || 1e-9))) });
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d > 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgG = (avgG * (period - 1) + g) / period;
    avgL = (avgL * (period - 1) + l) / period;
    out.push({ time: bars[i].time / 1000, value: 100 - 100 / (1 + (avgG / (avgL || 1e-9))) });
  }
  return out;
}

export type Point = LinePoint;

export function macd(bars: { time: number; close: number }[], fast = 12, slow = 26, signal = 9) {
  const fastEma = ema(bars.map((b) => b.close), fast);
  const slowEma = ema(bars.map((b) => b.close), slow);
  const macdLine = fastEma.map((f, i) => {
    const s = slowEma.find(s => s.time === f.time);
    return s ? { time: f.time, value: f.value - s.value } : { time: f.time, value: 0 };
  });
  const signalLine = ema(macdLine.map(p => p.value), signal);
  const histogram = macdLine.map((m, i) => {
    const s = signalLine.find(s => s.time === m.time);
    return { time: m.time, value: s ? m.value - s.value : 0 };
  });
  
  return {
    macd: macdLine,
    signal: signalLine,
    histogram,
  };
}

export function bollinger(bars: { time: number; close: number }[], period = 20, mult = 2) {
  return bollingerBands(bars, period, mult);
}
