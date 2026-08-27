// Data-source abstraction. Components depend ONLY on MarketDataSource.
// Tonight: MockMarketDataSource. Tomorrow: ApiMarketDataSource (real backend)
// implements the same interface — no component changes required.

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
 * Replace with ApiMarketDataSource later (same surface).
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
    return mock.mockFeedHealth("FYERS");
  }
  async getPipeline() {
    return mock.mockPipeline();
  }
}

// Single app-wide data source. Swap this line for the real implementation.
export const dataSource: MarketDataSource = new MockMarketDataSource();
