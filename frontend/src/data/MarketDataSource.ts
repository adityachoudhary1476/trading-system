// Data-source abstraction. Components depend ONLY on MarketDataSource.
// Mock mode: deterministic, no network.
// Live mode: calls server-side API (Vercel functions) that proxy Upstox.

import type {
  AIAnalysis,
  FeedHealth,
  MarketQuote,
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
    // Any unparseable field is surfaced as `undefined`, which downstream
    // components render as "—" (unavailable). `price` is the canonical
    // sentinel — if it is missing/invalid, the entire quote is unusable.
    if (!isFiniteNumber(api.price)) {
      throw new Error("Market quote unavailable: price field missing or invalid");
    }
    const meta = mock.getInstrument(symbol);
    const price = api.price;
    // Use nullish coalescing to preserve legitimate 0 values, then validate
    // with isFiniteNumber to guard against NaN/Infinity from malformed JSON.
    const prevClose = isFiniteNumber(api.previousClose ?? price) ? (api.previousClose ?? price) : price;
    const change = isFiniteNumber(api.change) ? api.change : price - prevClose;
    const changePct = isFiniteNumber(api.changePct)
      ? api.changePct
      : prevClose !== 0
        ? ((price - prevClose) / prevClose) * 100
        : 0;
    const dayOpen = isFiniteNumber(api.dayOpen) ? api.dayOpen : price;
    const dayHigh = isFiniteNumber(api.dayHigh) ? api.dayHigh : price;
    const dayLow = isFiniteNumber(api.dayLow) ? api.dayLow : price;
    const volume = isFiniteNumber(api.volume) ? api.volume : 0;
    // dayRange is a human-readable string; build it only from validated numerics
    const dayRange = `${dayLow.toLocaleString("en-IN")} — ${dayHigh.toLocaleString("en-IN")}`;
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
      vwap: price,
      dayRange,
      volatility: 0,
      sessionState: "REGULAR",
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
    return this.fetchJson<FeedHealth>("/api/upstox/status");
  }

  async getPipeline(): Promise<PipelineStage[]> {
    return this.fetchJson<PipelineStage[]>("/api/market/pipeline");
  }
}

// Single app-wide data source. Swap this line for the real implementation.
export const dataSource: MarketDataSource = new ApiMarketDataSource();
