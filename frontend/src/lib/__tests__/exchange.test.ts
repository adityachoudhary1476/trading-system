// @vitest-environment node
import { describe, it, expect, vi, beforeEach } from "vitest";

describe("exchangeCodeForToken", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  it("returns access_token on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ access_token: "token-xyz" }),
    });
    const { exchangeCodeForToken } = await import("../../../api/upstox/callback");
    const token = await exchangeCodeForToken({
      code: "auth-code",
      clientId: "cid",
      clientSecret: "secret",
      redirectUri: "https://x/cb",
    });
    expect(token).toBe("token-xyz");
  });

  it("throws on HTTP error", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({}),
    });
    const { exchangeCodeForToken, TokenExchangeError } = await import("../../../api/upstox/callback");
    await expect(
      exchangeCodeForToken({ code: "c", clientId: "cid", clientSecret: "s", redirectUri: "r" }),
    ).rejects.toBeInstanceOf(TokenExchangeError);
  });

  it("throws when access_token missing", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    });
    const { exchangeCodeForToken } = await import("../../../api/upstox/callback");
    await expect(
      exchangeCodeForToken({ code: "c", clientId: "cid", clientSecret: "s", redirectUri: "r" }),
    ).rejects.toThrow();
  });
});
