import { describe, it, expect, beforeAll, vi } from "vitest";

const getUser = vi.fn();
vi.mock("../../lib/supabase", () => ({
  getServerSupabase: () => ({
    auth: { getUser },
  }),
}));

const fetchMock = vi.fn();
beforeAll(() => {
  (globalThis as { fetch: unknown }).fetch = fetchMock;
});

import handler from "../status";

function makeReq(headers: Record<string, string> = {}) {
  return {
    method: "GET",
    headers,
    query: {},
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
}

describe("Status handler — happy path", () => {
  it("returns backend market status response", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({
        market: "NSE",
        phase: "regular",
        serverTime: 1700000000000,
        nextOpen: 1700000000000,
        nextClose: 1700001000000,
      }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(200);
    const body = res.body as Record<string, unknown>;
    expect(body.market).toBe("NSE");
    expect(body.phase).toBe("regular");
    expect(body.serverTime).toBe(1700000000000);
  });
});

describe("Status handler — auth", () => {
  it("returns 401 when Authorization header is missing", async () => {
    const req = makeReq({});
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(401);
    expect((res.body as { error: string }).error).toBe("unauthorized");
  });

  it("returns 401 when Supabase auth fails", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: { message: "bad jwt" } });
    const req = makeReq({ authorization: "Bearer bad-jwt" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(401);
    expect((res.body as { error: string }).error).toBe("unauthorized");
  });
});

describe("Status handler — method guard", () => {
  it("returns 405 for non-GET methods", async () => {
    await setupAuthed();
    const req = makeReq({ authorization: "Bearer fake-jwt" });
    (req as any).method = "POST";
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(405);
    expect((res.body as { error: string }).error).toBe("method_not_allowed");
  });
});

describe("Status handler — backend errors", () => {
  it("returns 502 backend_unavailable on backend error", async () => {
    await setupAuthed();
    fetchMock.mockResolvedValue({
      status: 500,
      ok: false,
      headers: { get: () => "application/json" },
      json: async () => ({ detail: "internal error" }),
    });
    const req = makeReq({ authorization: "Bearer fake-jwt" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(500);
    expect((res.body as { error: string }).error).toBe("status_error");
  });

  it("returns 502 on fetch failure", async () => {
    await setupAuthed();
    fetchMock.mockRejectedValue(new Error("network down"));
    const req = makeReq({ authorization: "Bearer fake-jwt" });
    const res = makeRes();
    await handler(req, res);
    expect(res.statusCode).toBe(502);
    expect((res.body as { error: string }).error).toBe("backend_unavailable");
  });
});
