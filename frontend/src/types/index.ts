// Shared frontend type contracts.
// These mirror the backend models (MarketSnapshot, MarketView, Signal,
// FeedStatus / FeedHealth) so the eventual real backend can fill them without
// rewriting the UI. See FRONTEND_BACKEND_CONTRACT.md for the field mapping.

export type Direction = "bullish" | "bearish" | "neutral" | "choppy";
export type SignalDirection = "long" | "short" | "hold" | "no_signal";
export type FeedStatus =
  | "healthy"
  | "stale"
  | "disconnected"
  | "auth_error"
  | "invalid_data";

/** A single OHLCV candle. Times are epoch ms (UTC) for charting. */
export interface OHLCVBar {
  /** epoch milliseconds (UTC) — charting libraries expect ms. */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Top-of-book quote for an instrument. */
export interface MarketQuote {
  symbol: string; // internal symbol, e.g. "NSE:SBIN"
  providerSymbol: string; // e.g. "NSE:SBIN-EQ"
  name: string;
  exchange: string; // e.g. "NSE"
  instrumentType: "index" | "equity" | "future" | "option";
  price: number;
  previousClose: number;
  change: number; // absolute
  changePct: number; // percent
  dayOpen: number;
  dayHigh: number;
  dayLow: number;
  volume: number;
  vwap: number;
  dayRange: string; // human, e.g. "1020.10 — 1061.80"
  volatility: number; // annualized-ish mock metric
  sessionState: "PRE_MARKET" | "REGULAR" | "POST_MARKET" | "CLOSED";
  lastUpdate: number; // epoch ms
}

/** Compact watchlist row. */
export interface WatchlistItem {
  symbol: string;
  name: string;
  price: number;
  change: number;
  changePct: number;
}

/** Structured AI analysis (maps backend MarketView + snapshot indicators). */
export interface AIAnalysis {
  symbol: string;
  timeframe: string;
  bias: Direction;
  confidence: number; // 0..1 ANALYTICAL (not a probability of profit)
  signal: SignalDirection;
  summary: string; // natural-language explanation
  factors: {
    label: string; // e.g. "Momentum"
    value: string; // e.g. "Positive"
    tone: "positive" | "negative" | "neutral" | "warning";
  }[];
  generatedAt: number; // epoch ms
  model: string;
}

/** A trading signal (analytical only — NEVER execution). */
export interface Signal {
  id: string;
  symbol: string;
  direction: SignalDirection; // long | short | hold | no_signal
  confidence: number; // 0..1
  generatedAt: number; // epoch ms
  price: number;
  bias: Direction;
  reason: string;
  source: string; // e.g. "deterministic"
}

/** Feed health (maps backend DataHealthMonitor.snapshot). */
export interface FeedHealth {
  feed: string; // e.g. "FYERS"
  status: FeedStatus;
  lastTick: number | null; // epoch ms
  eventsReceived: number;
  eventsRejected: number;
  candlesGenerated: number;
  lastClosedCandle: number | null; // epoch ms
  connected: boolean;
}

/** One stage in the pipeline architecture view. */
export interface PipelineStage {
  id: string;
  label: string;
  status: "connected" | "healthy" | "ready" | "disconnected" | "stale" | "auth_error" | "invalid_data";
  lastActivity: number | null;
  metric: string;
}

/** Market status summary. */
export interface MarketStatus {
  market: string; // "NSE"
  session: "PRE_MARKET" | "REGULAR" | "POST_MARKET" | "CLOSED";
  hours: string; // "09:15 — 15:30 IST"
  open: boolean;
}

/** App-wide runtime environment (exposed to UI; never secrets). */
export interface AppEnvironment {
  mode: "mock" | "live";
  environment: "development" | "production";
  dataSource: "Mock" | "API" | "WebSocket";
  execution: "DISABLED" | "ENABLED";
}
