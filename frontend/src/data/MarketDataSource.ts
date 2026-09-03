// Data-source abstraction. Components depend ONLY on MarketDataSource.
// Mock mode: deterministic, no network.
// Live mode: calls server-side API (Vercel functions) that proxy Upstox.

import type {
  AIAnalysis,
  FeedHealth,
  MarketQuote,
  MarketStatus,
  OHLCVBar,
  PipelineStage,
  Signal,
} from "@/types";
import * as mock from "@/data/mock";
import { getSupabaseClient } from "@/lib/supabase";
import { isFiniteNumber } from "@/lib/format";

export interface MarketDataSource {
  readonly mode: "mock" | "live";
  getQuote(symbol: string): Promise<MarketQuote>;
  getOHLCV(symbol: string, timeframe: string, bars?: number): Promise<OHLCVBar[]>;
  getAIAnalysis(symbol: string): Promise<AIAnalysis>;
  getSignals(limit?: number): Promise<Signal[]>;
  getFeedHealth(): Promise<FeedHealth>;
  getPipeline(): Promise<PipelineStage[]>;
  /**
   * Authoritative Indian market session status.
   * Returns the server-computed phase (pre_market / regular /
   * post_market / closed / holiday) plus server clock and next
   * session boundaries.  May throw on network failure.
   */
  getMarketStatus(): Promise<MarketStatus>;
}

/**
 * Mock implementation — deterministic, no network.
 */
export class MockMarketDataSource implements MarketDataSource {
  readonly mode = "mock" as const;
  async getQuote(symbol: string) {
    return mock.mockQuote(symbol);
  }
  async getOHLCV(symbol: string, timeframe: string, bars = 160) {
    return mock.mockOHLCV(symbol, timeframe, bars);
  }
  async getAIAnalysis(symbol: string) {
    return mock.mockAIAnalysis(symbol);
  }
  async getSignals(limit = 18) {
    return mock.mockSignals(limit);
  }
  async getFeedHealth() {
    return mock.mockFeedHealth("Upstox");
  }
  async getPipeline() {
    return mock.mockPipeline();
  }
  async getMarketStatus() {
    // Mock data source: derive a phase from the local browser clock.
    const now = new Date();
    const ist = new Date(now.getTime() + (5.5 - -now.getTimezoneOffset() / 60) * 3600_000);
    const day = ist.getUTCDay();
    const mins = ist.getUTCHours() * 60 + ist.getUTCMinutes();
    let phase: "pre_market" | "regular" | "post_market" | "closed" | "holiday" = "closed";
    if (day >= 1 && day <= 5) {
      if (mins < 9 * 60) phase = "closed";
      else if (mins < 9 * 60 + 15) phase = "pre_market";
      else if (mins <= 15 * 60 + 30) phase = "regular";
      else if (mins < 16 * 60) phase = "post_market";
      else phase = "closed";
    }
    return {
      market: "NSE",
      phase,
      serverTime: Date.now(),
      nextOpen: null,
      nextClose: null,
    } as MarketStatus;
  }
}

interface ApiQuoteResponse {
  symbol: string;
  price: number;
  previousClose: number;
  change: number;
  changePct: number;
  dayOpen: number;
  dayHigh: number;
  dayLow: number;
  volume: number;
  vwap: number;
  volatility: number;
  sessionState: "PRE_MARKET" | "REGULAR" | "POST_MARKET" | "CLOSED";
  lastUpdate: number;
}

/**
 * Live implementation — calls server-side API.
 * All broker credentials remain server-side; the browser only receives
 * normalized market data or non-sensitive error states.
 */
export class ApiMarketDataSource implements MarketDataSource {
  readonly mode = "live" as const;

  private async fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
    const sb = getSupabaseClient();
    const { data: sessionData } = await sb.auth.getSession();
    const token = sessionData.session?.access_token;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (init?.headers) {
      if (init.headers instanceof Headers) {
        init.headers.forEach((value, key) => {
          headers[key] = value;
        });
      } else {
        Object.assign(headers, init.headers as Record<string, string>);
      }
    }
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    let resp: Response;
    try {
      resp = await fetch(path, {
        ...init,
        headers,
        cache: "no-store",
      });
    } catch {
      throw new Error("Service unavailable: network request failed");
    }
    if (resp.status === 401) {
      throw new Error("Authentication required: please sign in");
    }
    if (resp.status === 403) {
      throw new Error("Authorization forbidden: insufficient permissions");
    }
    if (resp.status === 404) {
      throw new Error(`API endpoint not found: ${path}`);
    }
    if (resp.status >= 500) {
      throw new Error(`Backend server error: ${resp.status}`);
    }
    if (!resp.ok) {
      throw new Error(`API request failed: ${resp.status}`);
    }
    const contentType = resp.headers.get("content-type") ?? "";
    if (!contentType.includes("application/json")) {
      throw new Error("API routing error: expected JSON response");
    }
    return (await resp.json()) as T;
  }

  async getQuote(symbol: string): Promise<MarketQuote> {
    const api = await this.fetchJson<ApiQuoteResponse>(
      `/api/market/quote?symbol=${encodeURIComponent(symbol)}`,
    );
    // Runtime validation: the Upstox/Vercel API may return HTTP 200 with
    // missing, null, or malformed numeric fields. We do NOT fabricate values.
    // `price` is required — if it is missing/invalid, the quote is unusable.
    if (!isFiniteNumber(api.price)) {
      throw new Error("Market quote unavailable: price field missing or invalid");
    }
    const meta = mock.getInstrument(symbol);
    const price = api.price;
    // For optional fields, preserve the actual value when it is a valid
    // finite number (including 0). Otherwise leave as `undefined` — the
    // formatting helpers render `undefined` as "—".
    const prevClose = isFiniteNumber(api.previousClose) ? api.previousClose : undefined;
    // change/changePct may only be calculated when both price and prevClose
    // are valid numbers.
    const change = isFiniteNumber(api.change)
      ? api.change
      : prevClose !== undefined
        ? price - prevClose
        : undefined;
    const changePct = isFiniteNumber(api.changePct)
      ? api.changePct
      : change !== undefined && prevClose !== undefined && prevClose !== 0
        ? (change / prevClose) * 100
        : undefined;
    const dayOpen = isFiniteNumber(api.dayOpen) ? api.dayOpen : undefined;
    const dayHigh = isFiniteNumber(api.dayHigh) ? api.dayHigh : undefined;
    const dayLow = isFiniteNumber(api.dayLow) ? api.dayLow : undefined;
    const volume = isFiniteNumber(api.volume) ? api.volume : undefined;
    const vwap = isFiniteNumber(api.vwap) ? api.vwap : undefined;
    const volatility = isFiniteNumber(api.volatility) ? api.volatility : undefined;
    // dayRange is only constructed when both endpoints are actual values
    const dayRange =
      dayLow !== undefined && dayHigh !== undefined
        ? `${dayLow.toLocaleString("en-IN")} — ${dayHigh.toLocaleString("en-IN")}`
        : "—";
    return {
      symbol: api.symbol,
      providerSymbol: meta.providerSymbol,
      name: meta.name,
      exchange: meta.exchange,
      instrumentType: meta.instrumentType,
      price,
      previousClose: prevClose,
      change,
      changePct,
      dayOpen,
      dayHigh,
      dayLow,
      volume,
      vwap,
      dayRange,
      volatility,
      sessionState: api.sessionState ?? "REGULAR",
      lastUpdate: isFiniteNumber(api.lastUpdate) ? api.lastUpdate : Date.now(),
    };
  }

  async getOHLCV(symbol: string, timeframe: string, bars = 160): Promise<OHLCVBar[]> {
    const params = new URLSearchParams({ symbol, timeframe: timeframe, bars: String(bars) });
    return this.fetchJson<OHLCVBar[]>(`/api/market/ohlcv?${params.toString()}`);
  }

  async getAIAnalysis(symbol: string): Promise<AIAnalysis> {
    return this.fetchJson<AIAnalysis>(`/api/market/analysis?symbol=${encodeURIComponent(symbol)}`);
  }

  async getSignals(limit = 18): Promise<Signal[]> {
    return this.fetchJson<Signal[]>(`/api/market/signals?limit=${limit}`);
  }

  async getFeedHealth(): Promise<FeedHealth> {
    const raw = await this.fetchJson<Record<string, unknown>>("/api/upstox/status");
    // Map the Vercel /api/upstox/status response to the FeedHealth shape.
    // The endpoint returns {connected, provider, obtained_at, market, phase,
    // serverTime, nextOpen, nextClose}. We synthesise the fields the
    // DataHealthPanel expects from available data without fabricating
    // metrics we can't compute client-side.
    const connected = raw.connected === true;
    const obtained = typeof raw.obtained_at === "string" ? Date.parse(raw.obtained_at) : null;
    const isFiniteNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
    const serverTime = isFiniteNum(raw.serverTime) ? raw.serverTime : (obtained ?? null);
    return {
      feed: typeof raw.provider === "string" ? raw.provider : "Upstox",
      status: connected ? "healthy" : "disconnected",
      lastTick: serverTime,
      eventsReceived: 0,
      eventsRejected: 0,
      candlesGenerated: 0,
      lastClosedCandle: serverTime,
      connected,
    };
  }

  async getPipeline(): Promise<PipelineStage[]> {
    return this.fetchJson<PipelineStage[]>("/api/market/pipeline");
  }

  async getMarketStatus(): Promise<MarketStatus> {
    return this.fetchJson<MarketStatus>("/api/market/status");
  }
}

// Single app-wide data source. Swap this line for the real implementation.
export const dataSource: MarketDataSource = new ApiMarketDataSource();
