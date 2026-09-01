// Deterministic mock market data for FINOVA MARKETS frontend.
// Night-1 deliverable: the entire UI runs on this. The real backend (Python
// trading engine -> MarketSnapshot / Signal / FeedHealth over API/WebSocket)
// will replace MockMarketDataSource WITHOUT touching components (see
// FRONTEND_BACKEND_CONTRACT.md).
//
// IMPORTANT: numbers here are fabricated demo values. They are clearly flagged
// as MOCK in the UI (DEMO DATA banner). Never treat them as real market data.

import type {
  AIAnalysis,
  FeedHealth,
  MarketQuote,
  OHLCVBar,
  PipelineStage,
  Signal,
} from "@/types";

export interface InstrumentMeta {
  symbol: string; // internal, e.g. "NSE:SBIN"
  providerSymbol: string; // e.g. "NSE:SBIN-EQ"
  name: string;
  exchange: string;
  instrumentType: "index" | "equity" | "future" | "option";
  refPrice: number; // deterministic base used to derive mock values
}

/** Master list of instruments shown across the app. */
export const INSTRUMENTS: InstrumentMeta[] = [
  { symbol: "NSE:NIFTY50", providerSymbol: "NSE:NIFTY50-INDEX", name: "NIFTY 50", exchange: "NSE", instrumentType: "index", refPrice: 24842.15 },
  { symbol: "NSE:BANKNIFTY", providerSymbol: "NSE:BANKNIFTY-INDEX", name: "BANK NIFTY", exchange: "NSE", instrumentType: "index", refPrice: 51238.4 },
  { symbol: "NSE:FINNIFTY", providerSymbol: "NSE:FINNIFTY-INDEX", name: "FINNIFTY", exchange: "NSE", instrumentType: "index", refPrice: 23890.2 },
  { symbol: "NSE:SBIN", providerSymbol: "NSE:SBIN-EQ", name: "State Bank of India", exchange: "NSE", instrumentType: "equity", refPrice: 1051.2 },
  { symbol: "NSE:RELIANCE", providerSymbol: "NSE:RELIANCE-EQ", name: "Reliance Industries", exchange: "NSE", instrumentType: "equity", refPrice: 2947.85 },
  { symbol: "NSE:TCS", providerSymbol: "NSE:TCS-EQ", name: "Tata Consultancy Services", exchange: "NSE", instrumentType: "equity", refPrice: 4186.3 },
  { symbol: "NSE:INFY", providerSymbol: "NSE:INFY-EQ", name: "Infosys", exchange: "NSE", instrumentType: "equity", refPrice: 1932.7 },
];

export const WATCHLIST_SYMBOLS = [
  "NSE:NIFTY50",
  "NSE:BANKNIFTY",
  "NSE:SBIN",
  "NSE:RELIANCE",
  "NSE:TCS",
  "NSE:INFY",
];

// --- deterministic helpers -------------------------------------------------

/** Stable pseudo-random in [0,1) from an integer seed (mulberry32). */
function seededRand(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function getInstrument(symbol: string): InstrumentMeta {
  return INSTRUMENTS.find((i) => i.symbol === symbol) ?? INSTRUMENTS[0];
}

// --- quote -----------------------------------------------------------------

export function mockQuote(symbol: string): MarketQuote {
  const meta = getInstrument(symbol);
  const rng = seededRand(hashString(symbol) + 7);
  const prevClose = meta.refPrice;
  const changePct = (rng() - 0.42) * 2.4; // -1%..+1.2%-ish, slight upward bias
  const price = round(prevClose * (1 + changePct / 100), 2);
  const change = round(price - prevClose, 2);
  const dayHigh = round(price * (1 + rng() * 0.006), 2);
  const dayLow = round(price * (1 - rng() * 0.006), 2);
  const volume = Math.round(2_000_000 + rng() * 18_000_000);
  const vwap = round((dayHigh + dayLow + price) / 3, 2);
  const sessionState: MarketQuote["sessionState"] = meta.instrumentType === "index"
    ? "REGULAR"
    : "REGULAR";
  return {
    symbol: meta.symbol,
    providerSymbol: meta.providerSymbol,
    name: meta.name,
    exchange: meta.exchange,
    instrumentType: meta.instrumentType,
    price,
    previousClose: round(prevClose, 2),
    change,
    changePct: round(changePct, 2),
    dayOpen: round(prevClose * (1 + (rng() - 0.5) * 0.004), 2),
    dayHigh,
    dayLow,
    volume,
    vwap,
    dayRange: `${dayLow.toLocaleString("en-IN")} — ${dayHigh.toLocaleString("en-IN")}`,
    volatility: round(0.18 + rng() * 0.22, 3),
    sessionState,
    lastUpdate: Date.now(),
  };
}

// --- OHLCV -----------------------------------------------------------------

const TF_MINUTES: Record<string, number> = {
  "1m": 1,
  "5m": 5,
  "15m": 15,
  "1h": 60,
  "4h": 240,
  "1D": 1440,
};

export function mockOHLCV(symbol: string, timeframe: string, bars = 160): OHLCVBar[] {
  const meta = getInstrument(symbol);
  const rng = seededRand(hashString(symbol + timeframe) + 13);
  const mins = TF_MINUTES[timeframe] ?? 5;
  const now = Date.now();
  const step = mins * 60_000;
  // Align the last bar to a clean boundary for realism.
  const lastOpen = Math.floor(now / step) * step - step;
  let price = meta.refPrice * (0.97 + rng() * 0.06);
  const out: OHLCVBar[] = [];
  for (let i = bars - 1; i >= 0; i--) {
    const time = lastOpen - i * step;
    const drift = (rng() - 0.5) * 0.012;
    const open = price;
    const close = round(open * (1 + drift), 2);
    const high = round(Math.max(open, close) * (1 + rng() * 0.004), 2);
    const low = round(Math.min(open, close) * (1 - rng() * 0.004), 2);
    const volume = Math.round(200_000 + rng() * 2_400_000);
    out.push({ time, open: round(open, 2), high, low, close, volume });
    price = close;
  }
  return out;
}

// --- AI analysis -----------------------------------------------------------

const FACTOR_TEMPLATES = [
  { label: "Momentum", tone: "positive" as const, values: ["Positive", "Cooling", "Negative"] },
  { label: "Trend", tone: "positive" as const, values: ["Bullish", "Sideways", "Bearish"] },
  { label: "Volume", tone: "neutral" as const, values: ["Above average", "Average", "Below average"] },
  { label: "Volatility", tone: "warning" as const, values: ["Moderate", "Elevated", "Compressed"] },
];

export function mockAIAnalysis(symbol: string): AIAnalysis {
  const meta = getInstrument(symbol);
  const rng = seededRand(hashString(symbol) + 29);
  const roll = rng();
  const bias = roll > 0.62 ? "bullish" : roll > 0.34 ? "bearish" : roll > 0.18 ? "neutral" : "choppy";
  const confidence = round(0.58 + rng() * 0.32, 2);
  const signal =
    bias === "bullish" && confidence > 0.7 ? "long" : bias === "bearish" && confidence > 0.7 ? "short" : "hold";
  const factors = FACTOR_TEMPLATES.map((f) => ({
    label: f.label,
    value: f.values[Math.floor(rng() * f.values.length)],
    tone: f.tone,
  }));
  const summary =
    "Price holds above the short-term trend structure while participation supports the move. " +
    "Momentum is constructive, though the current extension argues for confirmation before a " +
    "stronger directional signal. Risk remains the prior session high acting as invalidation.";
  return {
    symbol: meta.symbol,
    timeframe: "5m",
    bias,
    confidence,
    signal,
    summary,
    factors,
    generatedAt: Date.now(),
    model: "mock-analyst-v1",
  };
}

// --- signals ---------------------------------------------------------------

const SIGNAL_DIRS: Signal["direction"][] = ["long", "short", "hold", "no_signal", "hold", "long"];

export function mockSignals(count = 18): Signal[] {
  const rng = seededRand(424242);
  const out: Signal[] = [];
  const now = Date.now();
  for (let i = 0; i < count; i++) {
    const meta = INSTRUMENTS[Math.floor(rng() * INSTRUMENTS.length)];
    const dir = SIGNAL_DIRS[Math.floor(rng() * SIGNAL_DIRS.length)];
    const confidence = round(0.55 + rng() * 0.4, 2);
    const price = round(meta.refPrice * (1 + (rng() - 0.5) * 0.02), 2);
    const reasons = [
      "Price > SMA20 and MACD > MACD signal (trend + momentum aligned)",
      "Bearish view; price below SMA20 with MACD crossover",
      "Low conviction — momentum mixed, awaiting confirmation",
      "Volume confirms the directional move above prior session high",
      "Neutral structure; no edge detected, staying flat",
    ];
    out.push({
      id: `sig-${(now - i * 1_700_000).toString(36)}-${i}`,
      symbol: meta.symbol,
      direction: dir,
      confidence,
      generatedAt: now - i * 1_700_000, // newest first
      price,
      bias: dir === "long" ? "bullish" : dir === "short" ? "bearish" : "neutral",
      reason: reasons[Math.floor(rng() * reasons.length)],
      source: "deterministic",
    });
  }
  return out.sort((a, b) => b.generatedAt - a.generatedAt);
}

// --- feed health -----------------------------------------------------------

export function mockFeedHealth(feed = "Upstox"): FeedHealth {
  const now = Date.now();
  return {
    feed,
    status: "healthy",
    lastTick: now - 1200,
    eventsReceived: 18432,
    eventsRejected: 3,
    candlesGenerated: 412,
    lastClosedCandle: now - 5 * 60_000,
    connected: true,
  };
}

// --- pipeline stages -------------------------------------------------------

export function mockPipeline(): PipelineStage[] {
  const now = Date.now();
  return [
    { id: "upstox", label: "Upstox", status: "connected", lastActivity: now - 1200, metric: "streaming" },
    { id: "events", label: "Market Events", status: "healthy", lastActivity: now - 900, metric: "18,432 events" },
    { id: "bus", label: "Event Bus", status: "healthy", lastActivity: now - 900, metric: "fan-out OK" },
    { id: "candles", label: "Candle Pipeline", status: "healthy", lastActivity: now - 5 * 60_000, metric: "412 candles" },
    { id: "health", label: "Data Health", status: "healthy", lastActivity: now - 1200, metric: "no stale" },
    { id: "snapshot", label: "Market Snapshot", status: "ready", lastActivity: now - 5 * 60_000, metric: "5m closed" },
    { id: "ai", label: "AI Analysis", status: "ready", lastActivity: now - 5 * 60_000, metric: "pending tick" },
    { id: "signals", label: "Signals", status: "ready", lastActivity: now - 8 * 60_000, metric: "WATCH" },
  ];
}

function round(v: number, d: number): number {
  const f = 10 ** d;
  return Math.round(v * f) / f;
}
