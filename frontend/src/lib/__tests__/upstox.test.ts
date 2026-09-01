import { describe, it, expect, vi, beforeEach } from "vitest";

const mockSession = { access_token: "test-jwt" };
const mockGetSession = vi.fn(async () => ({ data: { session: mockSession }, error: null }));
const mockSupabase = { auth: { getSession: mockGetSession } };

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => mockSupabase,
}));

describe("upstox lib", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("fetchConnectionStatus returns connected=false on 401", async () => {
    const { fetchConnectionStatus } = await import("../upstox");
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 401, ok: false });
    const result = await fetchConnectionStatus();
    expect(result.connected).toBe(false);
  });

  it("fetchConnectionStatus parses connected payload", async () => {
    const { fetchConnectionStatus } = await import("../upstox");
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ connected: true, provider: "upstox", obtained_at: "2026-01-01T00:00:00Z" }),
    });
    const result = await fetchConnectionStatus();
    expect(result.connected).toBe(true);
    expect(result.provider).toBe("upstox");
  });

  it("startUpstoxOAuth redirects to the authorization URL", async () => {
    const { startUpstoxOAuth } = await import("../upstox");
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ authorization_url: "https://api.upstox.com/auth?x=1", state: "abc", expires_at: "" }),
    });
    const assignSpy = vi.fn();
    Object.defineProperty(window, "location", { value: { href: "", assign: assignSpy }, writable: true });
    await startUpstoxOAuth();
    expect(window.location.href).toBe("https://api.upstox.com/auth?x=1");
  });

  it("startUpstoxOAuth throws when not authenticated", async () => {
    const { startUpstoxOAuth } = await import("../upstox");
    mockGetSession.mockResolvedValueOnce({ data: { session: undefined as unknown as null }, error: null });
    await expect(startUpstoxOAuth()).rejects.toThrow();
  });

  it("disconnectUpstox calls the disconnect endpoint", async () => {
    const { disconnectUpstox } = await import("../upstox");
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ disconnected: true }),
    });
    const result = await disconnectUpstox();
    expect(result.disconnected).toBe(true);
    expect(fetch).toHaveBeenCalledWith(
      "/api/upstox/disconnect",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("disconnectUpstox throws on failure", async () => {
    const { disconnectUpstox } = await import("../upstox");
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 500, ok: false });
    await expect(disconnectUpstox()).rejects.toThrow("Disconnect failed");
  });

  it("disconnectUpstox throws when not authenticated", async () => {
    const { disconnectUpstox } = await import("../upstox");
    mockGetSession.mockResolvedValueOnce({ data: { session: undefined as unknown as null }, error: null });
    await expect(disconnectUpstox()).rejects.toThrow();
  });
});
