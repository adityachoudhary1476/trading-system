import { describe, it, expect, vi, beforeEach } from "vitest";
import { ApiMarketDataSource } from "../MarketDataSource";

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
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ symbol: "NSE:SBIN", price: 100 }),
    });
    const result = await source.getQuote("NSE:SBIN");
    expect(fetch).toHaveBeenCalledWith(
      "/api/market/quote?symbol=NSE%3ASBIN",
      expect.any(Object),
    );
    expect(result.symbol).toBe("NSE:SBIN");
  });

  it("getOHLCV calls the correct endpoint with params", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
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
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ connected: true, provider: "upstox" }),
    });
    const result = await source.getFeedHealth();
    expect(fetch).toHaveBeenCalledWith(
      "/api/upstox/status",
      expect.any(Object),
    );
    expect(result.connected).toBe(true);
  });

  it("throws on non-ok response", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 500,
      ok: false,
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow("API request failed");
  });

  it("uses no-store cache", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({}),
    });
    await source.getSignals();
    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ cache: "no-store" }),
    );
  });
});
