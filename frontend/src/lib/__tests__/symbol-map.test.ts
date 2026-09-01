// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

describe("symbol-map: toUpstoxSymbol", () => {
  let toUpstoxSymbol: (s: string) => string;

  beforeEach(async () => {
    vi.resetModules();
    const mod = await import("../../../api/lib/symbol-map");
    toUpstoxSymbol = mod.toUpstoxSymbol;
  });

  it("maps NSE equity to NSE_EQ|SYMBOL", () => {
    expect(toUpstoxSymbol("NSE:SBIN")).toBe("NSE_EQ|SBIN");
    expect(toUpstoxSymbol("NSE:RELIANCE")).toBe("NSE_EQ|RELIANCE");
  });

  it("maps NSE index to NSE_INDEX|SYMBOL", () => {
    expect(toUpstoxSymbol("NSE:NIFTY50")).toBe("NSE_INDEX|NIFTY50");
    expect(toUpstoxSymbol("NSE:BANKNIFTY")).toBe("NSE_INDEX|BANKNIFTY");
  });

  it("throws on malformed symbol", () => {
    expect(() => toUpstoxSymbol("INVALID")).toThrow();
    expect(() => toUpstoxSymbol("A:B:C")).toThrow();
  });
});
