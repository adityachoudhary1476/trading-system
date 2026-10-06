import { describe, it, expect, vi, beforeEach } from "vitest";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const mockSession = { access_token: "test-jwt" };
const mockGetSession = vi.fn(async () => ({ data: { session: mockSession }, error: null }));
const mockSupabase = { auth: { getSession: mockGetSession } };

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => mockSupabase,
}));

function jsonResponse(status: number, body: unknown) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe("paperApi — error parsing", () => {
  let paperApi: typeof import("../paperApi").paperApi;

  beforeEach(async () => {
    vi.clearAllMocks();
    fetchMock.mockReset();
    paperApi = (await import("../paperApi")).paperApi;
  });

  it("surfaces the standard { error: { message } } envelope", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(501, {
        error: { code: "not_configured", message: "autonomous controller not configured" },
        timestamp: "t",
        schema_version: 1,
      }),
    );
    await expect(paperApi.getAutonomousBot()).rejects.toThrow(
      "autonomous controller not configured",
    );
  });

  it("surfaces FastAPI { detail: string } errors (e.g. 401 auth)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Authentication required" }));
    await expect(paperApi.getAutonomousBot()).rejects.toThrow("Authentication required");
  });

  it("joins pydantic validation detail arrays into a readable message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        detail: [
          { loc: ["body", "symbol"], msg: "field required", type: "value_error.missing" },
          { loc: ["body", "timeframe"], msg: "field required", type: "value_error.missing" },
        ],
      }),
    );
    await expect(paperApi.getAutonomousEvents({ limit: 5 })).rejects.toThrow(
      "field required; field required",
    );
  });

  it("captures raw text from non-JSON errors (500/proxy)", async () => {
    fetchMock.mockResolvedValue({
      status: 502,
      ok: false,
      headers: { get: () => "text/plain; charset=utf-8" },
      json: async () => {
        throw new Error("not json");
      },
      text: async () => "Bad Gateway: upstream backend unreachable",
    });
    await expect(paperApi.getAutonomousBot()).rejects.toThrow(
      "Bad Gateway: upstream backend unreachable",
    );
  });

  it("falls back to the HTTP status when the body is empty", async () => {
    fetchMock.mockResolvedValue({
      status: 500,
      ok: false,
      headers: { get: () => "text/plain; charset=utf-8" },
      json: async () => {
        throw new Error("not json");
      },
      text: async () => "",
    });
    await expect(paperApi.getAutonomousBot()).rejects.toThrow("Request failed with HTTP 500");
  });
});