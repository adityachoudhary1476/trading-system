import { describe, it, expect, vi, beforeEach } from "vitest";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const mockSession = { access_token: "test-jwt" };
const mockGetSession = vi.fn(async () => ({ data: { session: mockSession }, error: null }));
const mockSupabase = { auth: { getSession: mockGetSession } };

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => mockSupabase,
}));

describe("paperApi — autonomous endpoint contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchMock.mockReset();
  });

  it("getAutonomousBot uses GET", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ bot: { bot_id: "b", state: "running" } }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.getAutonomousBot();
    const init = fetchMock.mock.calls[0][1];
    expect(init.method ?? "GET").toBe("GET");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/paper/autonomous/bot");
  });

  it("getAutonomousScan uses GET", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ scan: {} }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.getAutonomousScan();
    const init = fetchMock.mock.calls[0][1];
    expect(init.method ?? "GET").toBe("GET");
  });

  it("getAutonomousEvents uses GET", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ events: [] }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.getAutonomousEvents({ limit: 5 });
    const init = fetchMock.mock.calls[0][1];
    expect(init.method ?? "GET").toBe("GET");
    expect(fetchMock.mock.calls[0][0]).toContain("limit=5");
  });

  it("getAutonomousDecisions uses POST, not GET", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ decisions: [] }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.getAutonomousDecisions();
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/paper/autonomous/decide");
  });

  it("setAutonomousBotLifecycle uses POST", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ success: true, message: "ok", bot: {} }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.setAutonomousBotLifecycle("start");
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/paper/autonomous/bot/start");
  });

  it("stopAutonomousDeployment uses POST", async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ status: "stopped", message: "ok" }),
    });
    const { paperApi } = await import("../paperApi");
    await paperApi.stopAutonomousDeployment("dep-123");
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/paper/autonomous/deployments/dep-123/stop",
    );
  });
});