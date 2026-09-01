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
    const resp = await fetch(path, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
    });
    if (!resp.ok) {
      throw new Error(`API request failed: ${resp.status}`);
    }
    return (await resp.json()) as T;
  }

  async getQuote(symbol: string): Promise<MarketQuote> {
    const api = await this.fetchJson<ApiQuoteResponse>(
      `/api/market/quote?symbol=${encodeURIComponent(symbol)}`,
    );
    const meta = mock.getInstrument(symbol);
    const price = api.price;
    const prevClose = api.previousClose ?? price;
    return {
      symbol: api.symbol,
      providerSymbol: meta.providerSymbol,
      name: meta.name,
      exchange: meta.exchange,
      instrumentType: meta.instrumentType,
      price,
      previousClose: prevClose,
      change: api.change ?? price - prevClose,
      changePct: api.changePct ?? (prevClose !== 0 ? ((price - prevClose) / prevClose) * 100 : 0),
      dayOpen: api.dayOpen ?? price,
      dayHigh: api.dayHigh ?? price,
      dayLow: api.dayLow ?? price,
      volume: api.volume ?? 0,
      vwap: price,
      dayRange: `${(api.dayLow ?? price).toLocaleString("en-IN")} — ${(api.dayHigh ?? price).toLocaleString("en-IN")}`,
      volatility: 0,
      sessionState: "REGULAR",
      lastUpdate: api.lastUpdate ?? Date.now(),
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
