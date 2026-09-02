import { describe, it, expect, beforeAll, vi } from "vitest";

const getUser = vi.fn();
const from = vi.fn();
vi.mock("../../lib/supabase", () => ({
  getServerSupabase: () => ({
    auth: { getUser },
    from,
  }),
}));

const fetchMock = vi.fn();
beforeAll(() => {
  (globalThis as { fetch: unknown }).fetch = fetchMock;
});

import handler from "../ohlcv";

const ENC_KEY = "test-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ORIGINAL_KEY = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ENC_KEY;

import { encryptToken } from "../../lib/crypto";

function makeReq(
  headers: Record<string, string> = {},
  query: Record<string, string> = {},
) {
  return {
    method: "GET",
    headers,
    query,
    body: undefined,
  } as unknown as Parameters<typeof handler>[0];
}

function makeRes() {
  const headers: Record<string, string> = {};
  return {
    statusCode: 0,
    body: undefined as unknown,
    headers,
    setHeader: (k: string, v: string) => {
      headers[k] = v;
    },
    status(code: number) {
      this.statusCode = code;
      return this;
    },
    json(payload: unknown) {
      this.body = payload;
      return this;
    },
  } as unknown as Parameters<typeof handler>[1];
}

async function setupAuthed() {
  getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
  const encrypted = encryptToken("upstox-test-token");
  from.mockReturnValue({
    select: () => ({
      eq: () => ({
        eq: () => ({
          maybeSingle: async () => ({
            data: { access_token_encrypted: encrypted },
            error: null,
          }),
        }),
      }),
    }),
  });
}

describe("OHLCV handler — happy path", () => {
  it("returns candles sorted oldest-first without fabricating values", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          candles: [
            // Upstox returns newest-first.
            ["2024-12-13T00:00:00+05:30", 810, 815, 805, 812, 1000, 0],
            ["2024-12-12T00:00:00+05:30", 800, 810, 798, 808, 900, 0],
            ["2024-12-11T00:00:00+05:30", 795, 805, 793, 800, 800, 0],
          ],
        },
      }),
    });
    const req = makeReq(
      { authorization: "Bearer jwt" },
      { symbol: "NSE:SBIN", timeframe: "1D", bars: "3" },
    );
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    const body = res.body as Array<{
      time: number;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }>;
    expect(body).toHaveLength(3);
    // Oldest-first ordering.
    expect(body[0].time).toBeLessThan(body[1].time);
    expect(body[1].time).toBeLessThan(body[2].time);
    expect(body[0].open).toBe(795);
    expect(body[0].close).toBe(800);
  });

  it("issues the Upstox V2 historical-candle URL with to_date BEFORE from_date", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: { candles: [] },
      }),
    });
    const req = makeReq(
      { authorization: "Bearer jwt" },
      { symbol: "NSE:SBIN", timeframe: "1D", bars: "5" },
    );
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    const calledUrl = fetchMock.mock.calls[0][0] as string;
    expect(calledUrl.startsWith("https://api.upstox.com/v2/historical-candle/")).toBe(true);
    const parts = calledUrl.split("/");
    // [https:, '', api.upstox.com, v2, historical-candle, {key}, {interval}, {to}, {from}]
    expect(parts[5]).toMatch(/^NSE_EQ/); // instrument key present
    expect(parts[6]).toBe("day"); // interval
    // Both dates are YYYY-MM-DD.
    expect(parts[7]).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(parts[8]).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    // to_date >= from_date.
    expect(parts[7] >= parts[8]).toBe(true);
  });
});

describe("OHLCV handler — no fabrication", () => {
  it("does not return more candles than the provider supplied", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          candles: [
            ["2024-12-12T00:00:00+05:30", 800, 810, 798, 808, 900, 0],
          ],
        },
      }),
    });
    const req = makeReq(
      { authorization: "Bearer jwt" },
      { symbol: "NSE:SBIN", timeframe: "1D", bars: "10" },
    );
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    expect((res.body as unknown[]).length).toBe(1); // we got 1, the handler returned 1
  });

  it("filters out malformed candles rather than fabricating fields", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          candles: [
            ["2024-12-13T00:00:00+05:30", 810, 815, 805, 812, 1000, 0],
            [null, 1, 2, 3, 4, 5], // malformed
            ["not-a-date", 1, 2, 3, 4, 5], // malformed
            ["2024-12-12T00:00:00+05:30", 800, 810, 798, 808, 900, 0],
          ],
        },
      }),
    });
    const req = makeReq(
      { authorization: "Bearer jwt" },
      { symbol: "NSE:SBIN", timeframe: "1D", bars: "4" },
    );
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    expect((res.body as unknown[]).length).toBe(2);
  });

  it("rejects unsupported timeframes with 400", async () => {
    const req = makeReq(
      { authorization: "Bearer jwt" },
      { symbol: "NSE:SBIN", timeframe: "5m", bars: "10" },
    );
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(400);
    expect((res.body as { error: string }).error).toBe("unsupported_timeframe");
  });
});

describe("OHLCV handler — token decryption failure", () => {
  it("returns 502 upstox_token_unreadable (NOT 500) on key mismatch", async () => {
    getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
    const encrypted = encryptToken("upstox-test-token");
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = "different-key-bbbbbbbbbbbbbbbbbb";
    try {
      from.mockReturnValue({
        select: () => ({
          eq: () => ({
            eq: () => ({
              maybeSingle: async () => ({
                data: { access_token_encrypted: encrypted },
                error: null,
              }),
            }),
          }),
        }),
      });
      const req = makeReq(
        { authorization: "Bearer jwt" },
        { symbol: "NSE:SBIN", timeframe: "1D" },
      );
      const res = makeRes();
      await handler(req, res);
      expect(res.statusCode).toBe(502);
      expect((res.body as { error: string }).error).toBe("upstox_token_unreadable");
    } finally {
      process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ENC_KEY;
    }
  });
});

process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ORIGINAL_KEY;
