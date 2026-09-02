import { describe, it, expect, vi, beforeEach } from "vitest";
import { ApiMarketDataSource } from "../MarketDataSource";
import { getSupabaseClient } from "@/lib/supabase";

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: vi.fn(),
}));

const mockGetSession = vi.fn();
vi.mocked(getSupabaseClient).mockReturnValue({
  auth: { getSession: mockGetSession },
} as unknown as ReturnType<typeof getSupabaseClient>);

const baseSuccess = {
  status: 200,
  ok: true,
  headers: { get: () => "application/json" },
  json: async () => ({}),
};

describe("ApiMarketDataSource.getQuote — runtime validation", () => {
  let source: ApiMarketDataSource;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", vi.fn());
    source = new ApiMarketDataSource();
    mockGetSession.mockResolvedValue({
      data: { session: { access_token: "test-token" } },
      error: null,
    });
  });

  it("returns a valid quote on well-formed API response", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 850.5,
        previousClose: 840.0,
        change: 10.5,
        changePct: 1.25,
        dayOpen: 845.0,
        dayHigh: 855.0,
        dayLow: 840.0,
        volume: 1500000,
        lastUpdate: 1704067200000,
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.price).toBe(850.5);
    expect(q.previousClose).toBe(840.0);
    expect(q.change).toBe(10.5);
    expect(q.changePct).toBe(1.25);
    expect(q.dayOpen).toBe(845.0);
    expect(q.dayHigh).toBe(855.0);
    expect(q.dayLow).toBe(840.0);
    expect(q.volume).toBe(1500000);
    expect(q.vwap).toBeUndefined(); // not in response → unavailable
    expect(q.volatility).toBeUndefined(); // not in response → unavailable
    expect(q.dayRange).toBe("840 — 855"); // constructed from dayLow/dayHigh
  });

  it("throws on HTTP 500", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 500,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow("Backend server error");
  });

  it("throws on HTTP 401", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 401,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow("Authentication required");
  });

  it("throws on HTTP 403", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 403,
      ok: false,
      headers: { get: () => "application/json" },
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow("Authorization forbidden");
  });

  it("throws when price is missing from a 200 response", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({ symbol: "NSE:SBIN" }), // price field absent
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow(
      "Market quote unavailable: price field missing or invalid",
    );
  });

  it("throws when price is null in a 200 response", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({ symbol: "NSE:SBIN", price: null }),
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow(
      "Market quote unavailable: price field missing or invalid",
    );
  });

  it("throws when price is NaN from malformed JSON", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({ symbol: "NSE:SBIN", price: "not-a-number" }),
    });
    await expect(source.getQuote("NSE:SBIN")).rejects.toThrow(
      "Market quote unavailable: price field missing or invalid",
    );
  });

  it("handles HTTP 200 with valid zero price", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 0,
        previousClose: 0,
        change: 0,
        changePct: 0,
        dayOpen: 0,
        dayHigh: 0,
        dayLow: 0,
        volume: 0,
        lastUpdate: 1704067200000,
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.price).toBe(0);
    expect(q.previousClose).toBe(0);
    expect(q.volume).toBe(0);
  });

  it("does NOT fabricate values when optional fields are missing (200 with partial body)", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        // price is present; all other numeric fields are absent
        price: 100,
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.price).toBe(100);
    // Optional fields must remain undefined — NEVER fabricated to price or 0
    expect(q.previousClose).toBeUndefined();
    expect(q.change).toBeUndefined();
    expect(q.changePct).toBeUndefined();
    expect(q.dayOpen).toBeUndefined();
    expect(q.dayHigh).toBeUndefined();
    expect(q.dayLow).toBeUndefined();
    expect(q.volume).toBeUndefined();
    expect(q.vwap).toBeUndefined();
    expect(q.volatility).toBeUndefined();
    expect(q.dayRange).toBe("—");
  });

  it("computes change/changePct only from actual price and previousClose", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 100,
        previousClose: 95,
        // change, changePct missing — computed from price - prevClose
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.change).toBe(5); // price - prevClose
    expect(q.changePct).toBeCloseTo((5 / 95) * 100, 5);
  });

  it("leaves dayRange as '—' when dayLow or dayHigh is missing", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 100,
        dayLow: 90,
        // dayHigh missing
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.dayRange).toBe("—");
  });

  it("constructs dayRange string from actual dayLow and dayHigh", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 100,
        dayLow: 90,
        dayHigh: 110,
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.dayRange).toBe("90 — 110");
  });

  it("preserves valid zero values for all numeric fields", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseSuccess,
      json: async () => ({
        symbol: "NSE:SBIN",
        price: 0,
        previousClose: 0,
        change: 0,
        changePct: 0,
        dayOpen: 0,
        dayHigh: 0,
        dayLow: 0,
        volume: 0,
        vwap: 0,
        volatility: 0,
        lastUpdate: 1704067200000,
      }),
    });
    const q = await source.getQuote("NSE:SBIN");
    expect(q.price).toBe(0);
    expect(q.previousClose).toBe(0);
    expect(q.change).toBe(0);
    expect(q.changePct).toBe(0);
    expect(q.dayOpen).toBe(0);
    expect(q.dayHigh).toBe(0);
    expect(q.dayLow).toBe(0);
    expect(q.volume).toBe(0);
    expect(q.vwap).toBe(0);
    expect(q.volatility).toBe(0);
    expect(q.dayRange).toBe("0 — 0");
  });
});
