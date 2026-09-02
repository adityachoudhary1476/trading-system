// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockSupabaseAuth = {
  getUser: vi.fn(),
};

const mockSupabaseFrom = vi.fn();

const mockDecryptToken = vi.fn();

vi.mock("../../../api/lib/supabase", () => ({
  getServerSupabase: () => ({
    auth: mockSupabaseAuth,
    from: mockSupabaseFrom,
  }),
}));

vi.mock("../../../api/lib/crypto", () => ({
  decryptToken: mockDecryptToken,
}));

describe("market/quote API", () => {
  let handler: (req: any, res: any) => Promise<void>;

  beforeEach(async () => {
    vi.resetModules();
    vi.clearAllMocks();
    mockSupabaseFrom.mockReturnValue({
      select: vi.fn().mockReturnThis(),
      eq: vi.fn().mockReturnThis(),
      maybeSingle: vi.fn().mockResolvedValue({
        data: { access_token_encrypted: "encrypted-blob" },
        error: null,
      }),
    });
    mockSupabaseAuth.getUser.mockResolvedValue({
      data: { user: { id: "user-123" } },
      error: null,
    });
    mockDecryptToken.mockReturnValue("mock-access-token");

    const mod = await import("../../../api/market/quote");
    handler = mod.default;
  });

  it("returns 405 for non-GET methods", async () => {
    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler({ method: "POST", headers: {}, query: {} }, res);
    expect(res.status).toHaveBeenCalledWith(405);
  });

  it("returns 400 for missing/invalid symbol", async () => {
    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler({ method: "GET", headers: {}, query: {} }, res);
    expect(res.status).toHaveBeenCalledWith(400);
  });

  it("returns 401 without bearer token", async () => {
    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler({ method: "GET", headers: {}, query: { symbol: "NSE:SBIN" } }, res);
    expect(res.status).toHaveBeenCalledWith(401);
  });

  it("returns 403 when Upstox not connected", async () => {
    mockSupabaseFrom.mockReturnValue({
      select: vi.fn().mockReturnThis(),
      eq: vi.fn().mockReturnThis(),
      maybeSingle: vi.fn().mockResolvedValue({ data: null, error: null }),
    });
    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN" } },
      res,
    );
    expect(res.status).toHaveBeenCalledWith(403);
  });

  it("returns normalized quote on success", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        data: {
          "NSE_EQ:INE062A01020": {
            last_price: 850.5,
            open: 845.0,
            high: 855.0,
            low: 840.0,
            close: 848.0,
            prev_close: 840.0,
            volume: 1500000,
          },
        },
      }),
    });

    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(200);
    expect(res.json).toHaveBeenCalledWith(
      expect.objectContaining({
        symbol: "NSE:SBIN",
        price: 850.5,
        previousClose: 840.0,
        change: 10.5,
        volume: 1500000,
      }),
    );
  });

  it("returns 403 on expired Upstox token", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
    });

    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(403);
  });

  it("returns 502 on Upstox API error", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    });

    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(502);
  });
});

describe("market/ohlcv API", () => {
  let handler: (req: any, res: any) => Promise<void>;

  beforeEach(async () => {
    vi.resetModules();
    vi.clearAllMocks();
    mockSupabaseFrom.mockReturnValue({
      select: vi.fn().mockReturnThis(),
      eq: vi.fn().mockReturnThis(),
      maybeSingle: vi.fn().mockResolvedValue({
        data: { access_token_encrypted: "encrypted-blob" },
        error: null,
      }),
    });
    mockSupabaseAuth.getUser.mockResolvedValue({
      data: { user: { id: "user-123" } },
      error: null,
    });
    mockDecryptToken.mockReturnValue("mock-access-token");

    const mod = await import("../../../api/market/ohlcv");
    handler = mod.default;
  });

  it("returns normalized OHLCV bars on success", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        data: {
          candles: [
            [1704067200, 840.0, 850.0, 835.0, 848.0, 1000000, 0],
            [1704153600, 848.0, 855.0, 845.0, 850.5, 1500000, 0],
          ],
        },
      }),
    });

    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN", timeframe: "1d", bars: "2" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(200);
    const jsonArg = (res.json as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(jsonArg).toHaveLength(2);
    expect(jsonArg[0]).toEqual({
      time: 1704067200000,
      open: 840.0,
      high: 850.0,
      low: 835.0,
      close: 848.0,
      volume: 1000000,
    });
  });

  it("returns 400 for invalid timeframe", async () => {
    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN", timeframe: "invalid" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(400);
  });

  it("returns 403 on expired Upstox token", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
    });

    const res = { setHeader: vi.fn(), status: vi.fn().mockReturnThis(), json: vi.fn() };
    await handler(
      { method: "GET", headers: { authorization: "Bearer valid-token" }, query: { symbol: "NSE:SBIN", timeframe: "1d" } },
      res,
    );

    expect(res.status).toHaveBeenCalledWith(403);
  });
});
