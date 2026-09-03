import { describe, it, expect, vi, beforeEach } from "vitest";
import { ApiMarketDataSource } from "../MarketDataSource";

vi.mock("../../lib/supabase", () => ({
  getSupabaseClient: vi.fn(),
}));

import { getSupabaseClient } from "../../lib/supabase";

const mockGetSession = vi.fn();

vi.mocked(getSupabaseClient).mockReturnValue({
  auth: { getSession: mockGetSession },
} as unknown as ReturnType<typeof getSupabaseClient>);

describe("ApiMarketDataSource", () => {
  let source: ApiMarketDataSource;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", vi.fn());
    source = new ApiMarketDataSource();
  });

  it("has mode 'live'", () => {
    expect(source.mode).toBe("live");
  });

  it("getQuote calls the correct endpoint", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ symbol: "NSE:SBIN", price: 100 }),
    });
    const result = await source.getQuote("NSE:SBIN");
    expect(fetch).toHaveBeenCalledWith(
      "/api/market/quote?symbol=NSE%3ASBIN",
      expect.any(Object),
    );
    expect(result.symbol).toBe("NSE:SBIN");
  });

  it("sends Authorization header when session exists", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ symbol: "NSE:SBIN", price: 100 }),
    });
    await source.getQuote("NSE:SBIN");
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[1].headers).toHaveProperty("Authorization", "Bearer test-token");
  });

  it("omits Authorization header when no session exists", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: null },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ symbol: "NSE:SBIN", price: 100 }),
    });
    await source.getQuote("NSE:SBIN");
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[1].headers).not.toHaveProperty("Authorization");
  });

  it("getOHLCV calls the correct endpoint with params", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => [],
    });
    await source.getOHLCV("NSE:SBIN", "1d", 100);
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[0]).toMatch(/^\/api\/market\/ohlcv\?/);
    const url = new URL(callArgs[0], "http://localhost");
    expect(url.searchParams.get("symbol")).toBe("NSE:SBIN");
    expect(url.searchParams.get("timeframe")).toBe("1d");
    expect(url.searchParams.get("bars")).toBe("100");
  });

  it("getFeedHealth calls /api/upstox/status", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ connected: true, provider: "upstox" }),
    });
    const result = await source.getFeedHealth();
    expect(fetch).toHaveBeenCalledWith(
      "/api/upstox/status",
      expect.any(Object),
    );
    expect(result.connected).toBe(true);
  });

  it("getMarketStatus calls /api/market/status", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({
        market: "NSE",
        phase: "regular",
        serverTime: 1700000000000,
        nextOpen: null,
        nextClose: null,
      }),
    });
    await source.getMarketStatus();
    expect(fetch).toHaveBeenCalledWith(
      "/api/market/status",
      expect.any(Object),
    );
  });

  it("throws on non-ok response", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 500,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow("Backend server error");
  });

  it("uses no-store cache", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({}),
    });
    await source.getSignals();
    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  // --- Auth flow verification for Analysis and Signals (Issue 2) ---
  it("getAIAnalysis sends Authorization Bearer header", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ symbol: "NSE:NIFTY50", bias: "neutral", confidence: 0.5, signal: "no_signal", summary: "", factors: [], generatedAt: 0, model: "test", timeframe: "1d" }),
    });
    await source.getAIAnalysis("NSE:NIFTY50");
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[1].headers).toHaveProperty("Authorization", "Bearer test-token");
  });

  it("getAIAnalysis throws on 401 (authentication required)", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 401,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getAIAnalysis("NSE:NIFTY50")).rejects.toThrow(
      "Authentication required: please sign in",
    );
  });

  it("getSignals sends Authorization Bearer header", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => [],
    });
    await source.getSignals(12);
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[0]).toMatch(/\/api\/market\/signals\?limit=12/);
    expect(callArgs[1].headers).toHaveProperty("Authorization", "Bearer test-token");
  });

  it("getSignals throws on 401 (authentication required)", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 401,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getSignals(12)).rejects.toThrow(
      "Authentication required: please sign in",
    );
  });

  it("omits Authorization header when no session for getAIAnalysis", async () => {
    mockGetSession.mockResolvedValue({
      data: { session: null },
      error: null,
    });
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 401,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getAIAnalysis("NSE:NIFTY50")).rejects.toThrow(
      "Authentication required: please sign in",
    );
    const callArgs = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(callArgs[1].headers).not.toHaveProperty("Authorization");
  });
});
