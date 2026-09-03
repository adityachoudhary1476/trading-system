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

/** Top-of-book quote for an instrument.
 * `price` is the canonical sentinel — it is always present (validated at the API boundary).
 * All other numeric fields are optional; they may be `undefined` when the
 * upstream API returns them as missing/null/invalid. Downstream formatting
 * helpers render `undefined` as "—" (unavailable).
 */
export interface MarketQuote {
  symbol: string; // internal symbol, e.g. "NSE:SBIN"
  providerSymbol: string; // e.g. "NSE:SBIN-EQ"
  name: string;
  exchange: string; // e.g. "NSE"
  instrumentType: "index" | "equity" | "future" | "option";
  price: number; // required — validated at API boundary
  previousClose: number | undefined;
  change: number | undefined; // absolute
  changePct: number | undefined; // percent
  dayOpen: number | undefined;
  dayHigh: number | undefined;
  dayLow: number | undefined;
  volume: number | undefined;
  vwap: number | undefined;
  dayRange: string; // human, e.g. "1020.10 — 1061.80", or "—" when unavailable
  volatility: number | undefined; // annualized-ish metric
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
  // New intelligence fields
  horizon?: "intraday" | "short_term" | "swing";
  expectedMove?: {
    lowerPct: number;
    upperPct: number;
    basis: "atr" | "volatility";
  };
  evidence?: {
    positive: string[];
    negative: string[];
    neutral: string[];
    agreement: "strong" | "moderate" | "mixed" | "neutral";
  };
  invalidation?: string;
  instrumentClass?: "equity" | "index" | "future" | "option_ce" | "option_pe";
}

/** A trading signal (analytical only — NEVER execution).
 *
 * Wire-format contract (mirrors backend `SignalDTO`):
 *
 * | field         | type                                      | source                         |
 * |---------------|-------------------------------------------|--------------------------------|
 * | id            | string (uuid4)                            | backend                        |
 * | symbol        | string  (e.g. "NSE:SBIN")                 | backend                        |
 * | direction     | "long" | "short" | "hold" | "no_signal"   | strategy engine                 |
 * | confidence    | number in [0, 1]                          | strategy engine                 |
 * | price         | number > 0  (finite close of source bar)  | latest candle close from data  |
 * | bias          | Direction ("bullish" | "bearish" | "neutral" | "choppy") | AI market view         |
 * | reason        | string                                    | strategy reason                 |
 * | generatedAt   | number  (epoch ms of source bar)          | snapshot timestamp             |
 * | source        | string  (e.g. "deterministic")           | strategy identifier             |
 */
export interface Signal {
  id: string;
  symbol: string;
  direction: SignalDirection;
  confidence: number;
  price: number;
  bias: Direction;
  reason: string;
  generatedAt: number;
  source: string;
}

/** Feed health (maps backend DataHealthMonitor.snapshot). */
export interface FeedHealth {
  feed: string; // e.g. "Upstox"
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
  phase: "pre_market" | "regular" | "post_market" | "closed" | "holiday";
  serverTime: number; // epoch ms (UTC)
  nextOpen: number | null; // epoch ms (UTC)
  nextClose: number | null; // epoch ms (UTC)
}

/** App-wide runtime environment (exposed to UI; never secrets). */
export interface AppEnvironment {
  mode: "mock" | "live";
  environment: "development" | "production";
  dataSource: "Mock" | "API" | "WebSocket";
  execution: "DISABLED" | "ENABLED";
}
