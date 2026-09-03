import { describe, it, expect, beforeAll, vi } from "vitest";

// Mock the server-side Supabase client so we don't hit the network.
const getUser = vi.fn();
const from = vi.fn();
vi.mock("../../lib/supabase", () => ({
  getServerSupabase: () => ({
    auth: { getUser },
    from,
  }),
}));

// Mock global fetch (used for Upstox calls).
const fetchMock = vi.fn();
beforeAll(() => {
  (globalThis as { fetch: unknown }).fetch = fetchMock;
});

// Import AFTER mocks are installed.
import handler from "../quote";

const ENC_KEY = "test-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ORIGINAL_KEY = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ENC_KEY;

import { encryptToken } from "../../lib/crypto";

function makeReq(headers: Record<string, string> = {}, query: Record<string, string> = {}) {
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

describe("Quote handler — happy path", () => {
  it("returns genuine price and OHLC for a valid Upstox response", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          "NSE_EQ:SBIN": {
            last_price: 800.5,
            ohlc: { open: 798, high: 805, low: 797, close: 795 },
            volume: 1234567,
            prev_close: 795,
          },
        },
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    expect((res.headers as Record<string, string>)["Cache-Control"]).toContain("no-store");
    const body = res.body as Record<string, unknown>;
    expect(body.symbol).toBe("NSE:SBIN");
    expect(body.price).toBe(800.5);
    expect(body.previousClose).toBe(795);
    expect(body.change).toBe(5.5);
    expect(body.dayOpen).toBe(798);
    expect(body.dayHigh).toBe(805);
    expect(body.dayLow).toBe(797);
    expect(body.volume).toBe(1234567);
  });
});

describe("Quote handler — no fabrication", () => {
  it("does NOT substitute 0 when last_price is missing (returns 404)", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          "NSE_EQ:SBIN": { ohlc: { open: 1, high: 2, low: 0.5, close: 1.5 } },
        },
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(404);
    expect((res.body as { error: string }).error).toBe("upstox_price_unavailable");
  });

  it("does NOT substitute 0 for missing previousClose; emits no previousClose/change fields", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          "NSE_EQ:SBIN": { last_price: 100 },
        },
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    const body = res.body as Record<string, unknown>;
    expect(body.price).toBe(100);
    // No fabricated previousClose / change / changePct.
    expect("previousClose" in body).toBe(false);
    expect("change" in body).toBe(false);
    expect("changePct" in body).toBe(false);
  });

  it("does NOT substitute 0 for missing OHLC; omits dayOpen/dayHigh/dayLow", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          "NSE_EQ:SBIN": { last_price: 100 },
        },
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    const body = res.body as Record<string, unknown>;
    expect("dayOpen" in body).toBe(false);
    expect("dayHigh" in body).toBe(false);
    expect("dayLow" in body).toBe(false);
  });

  it("does NOT substitute 0 for missing volume; omits volume", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => ({
        status: "success",
        data: {
          "NSE_EQ:SBIN": { last_price: 100 },
        },
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    const body = res.body as Record<string, unknown>;
    expect("volume" in body).toBe(false);
  });
});

describe("Quote handler — token decryption failure", () => {
  it("falls through to service token: returns 403 upstox_not_connected when no env fallback is set and key is wrong", async () => {
    getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
    const encrypted = encryptToken("upstox-test-token");
    // Tamper: rotate the key after the token was encrypted.
    process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = "different-key-bbbbbbbbbbbbbbbbbb";
    delete process.env.UPSTOX_ACCESS_TOKEN;
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
      const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
      const res = makeRes();
      await handler(req, res);
      expect(res.statusCode).toBe(403);
      expect((res.body as { error: string }).error).toBe("upstox_not_connected");
    } finally {
      process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ENC_KEY;
    }
  });

  it("falls through to service token: returns 403 upstox_not_connected when no env fallback is set and key is missing", async () => {
    getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
    const encrypted = encryptToken("upstox-test-token");
    const original = process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    delete process.env.UPSTOX_TOKEN_ENCRYPTION_KEY;
    delete process.env.UPSTOX_ACCESS_TOKEN;
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
      const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
      const res = makeRes();
      await handler(req, res);
      expect(res.statusCode).toBe(403);
      expect((res.body as { error: string }).error).toBe("upstox_not_connected");
    } finally {
      if (original !== undefined) process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = original;
    }
  });

  it("uses UPSTOX_ACCESS_TOKEN env fallback when user has no connected broker", async () => {
    getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
    const savedToken = process.env.UPSTOX_ACCESS_TOKEN;
    process.env.UPSTOX_ACCESS_TOKEN = "service-account-token";
    try {
      from.mockReturnValue({
        select: () => ({
          eq: () => ({
            eq: () => ({
              maybeSingle: async () => ({ data: null, error: null }),
            }),
          }),
        }),
      });
      fetchMock.mockClear();
      fetchMock.mockResolvedValue({
        status: 200,
        ok: true,
        headers: new Map(),
        json: async () => ({
          status: "success",
          data: {
            "NSE_EQ:SBIN": {
              last_price: 800.5,
              prev_close: 795,
            },
          },
        }),
      });
      const req = makeReq({ authorization: "Bearer fake-jwt" }, { symbol: "NSE:SBIN" });
      const res = makeRes();
      await handler(req, res);
      expect(res.statusCode).toBe(200);
      expect((res.body as { price: number }).price).toBe(800.5);
      expect(fetchMock.mock.calls[0][1]?.headers?.Authorization).toBe(
        "Bearer service-account-token",
      );
    } finally {
      if (savedToken !== undefined) process.env.UPSTOX_ACCESS_TOKEN = savedToken;
      else delete process.env.UPSTOX_ACCESS_TOKEN;
    }
  });
});

describe("Quote handler — error mapping", () => {
  it("returns 401 when Authorization header is missing", async () => {
    const req = makeReq({}, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(401);
  });

  it("returns 401 when Supabase auth fails", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: { message: "bad jwt" } });
    const req = makeReq({ authorization: "Bearer bad-jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(401);
  });

  it("returns 403 upstox_not_connected when no row exists", async () => {
    getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
    from.mockReturnValue({
      select: () => ({
        eq: () => ({
          eq: () => ({
            maybeSingle: async () => ({ data: null, error: null }),
          }),
        }),
      }),
    });
    const req = makeReq({ authorization: "Bearer jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(403);
    expect((res.body as { error: string }).error).toBe("upstox_not_connected");
  });

  it("returns 403 upstox_token_expired on Upstox 401/403", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 401,
      ok: false,
      headers: new Map(),
      json: async () => ({}),
    });
    const req = makeReq({ authorization: "Bearer jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(403);
    expect((res.body as { error: string }).error).toBe("upstox_token_expired");
  });

  it("returns 429 with Retry-After on Upstox rate limit", async () => {
    await setupAuthed();
    const headers = new Map<string, string>([["retry-after", "5"]]);
    fetchMock.mockResolvedValue({
      status: 429,
      ok: false,
      headers: { get: (k: string) => headers.get(k.toLowerCase()) ?? null },
      json: async () => ({}),
    });
    const req = makeReq({ authorization: "Bearer jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(429);
    expect((res.body as { error: string }).error).toBe("upstox_rate_limited");
    expect((res.headers as Record<string, string>)["Retry-After"]).toBe("5");
  });

  it("returns 502 upstox_malformed_response on non-JSON Upstox response", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Map(),
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    });
    const req = makeReq({ authorization: "Bearer jwt" }, { symbol: "NSE:SBIN" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(502);
    expect((res.body as { error: string }).error).toBe("upstox_malformed_response");
  });
});

// Restore env at the very end of the suite so it doesn't leak into other files.
process.env.UPSTOX_TOKEN_ENCRYPTION_KEY = ORIGINAL_KEY;
