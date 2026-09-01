import { describe, it, expect } from "vitest";
import { validateOAuthState } from "../oauth-state-machine";

const FUTURE = new Date(Date.now() + 60_000).toISOString();
const PAST = new Date(Date.now() - 60_000).toISOString();

describe("validateOAuthState", () => {
  it("rejects when Upstox reports an error", async () => {
    const r = await validateOAuthState({
      state: "s",
      code: "c",
      upstoxError: "access_denied",
      getSession: async () => ({ row: null, error: null }),
    });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("access_denied");
  });

  it("rejects missing code", async () => {
    const r = await validateOAuthState({
      state: "s",
      code: "",
      upstoxError: "",
      getSession: async () => ({ row: null, error: null }),
    });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("missing_params");
  });

  it("rejects missing state", async () => {
    const r = await validateOAuthState({
      state: "",
      code: "c",
      upstoxError: "",
      getSession: async () => ({ row: null, error: null }),
    });
    expect(r.ok).toBe(false);
  });

  it("rejects unknown state", async () => {
    const r = await validateOAuthState({
      state: "unknown",
      code: "c",
      upstoxError: "",
      getSession: async () => ({ row: null, error: "not found" }),
    });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("invalid_state");
  });

  it("rejects already-consumed state", async () => {
    const r = await validateOAuthState({
      state: "s",
      code: "c",
      upstoxError: "",
      getSession: async () => ({
        row: { id: "1", user_id: "u1", expires_at: FUTURE, consumed_at: new Date().toISOString() },
        error: null,
      }),
    });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("state_reused");
  });

  it("rejects expired state", async () => {
    const r = await validateOAuthState({
      state: "s",
      code: "c",
      upstoxError: "",
      getSession: async () => ({
        row: { id: "1", user_id: "u1", expires_at: PAST, consumed_at: null },
        error: null,
      }),
    });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("state_expired");
  });

  it("accepts valid fresh state and returns userId + stateId", async () => {
    const r = await validateOAuthState({
      state: "s",
      code: "c",
      upstoxError: "",
      getSession: async () => ({
        row: { id: "sid-9", user_id: "user-42", expires_at: FUTURE, consumed_at: null },
        error: null,
      }),
    });
    expect(r.ok).toBe(true);
    expect(r.userId).toBe("user-42");
    expect(r.stateId).toBe("sid-9");
  });
});
