// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

describe("symbol-map: toUpstoxSymbol", () => {
  let toUpstoxSymbol: (s: string) => string;

  beforeEach(async () => {
    vi.resetModules();
    const mod = await import("../../../api/lib/symbol-map");
    toUpstoxSymbol = mod.toUpstoxSymbol;
  });

  it("maps NSE equity to ISIN-based V2 instrument key", () => {
    expect(toUpstoxSymbol("NSE:SBIN")).toBe("NSE_EQ|INE062A01020");
    expect(toUpstoxSymbol("NSE:RELIANCE")).toBe("NSE_EQ|INE002A01018");
    expect(toUpstoxSymbol("NSE:INFY")).toBe("NSE_EQ|INE009A01021");
    expect(toUpstoxSymbol("NSE:TCS")).toBe("NSE_EQ|INE007A01025");
    expect(toUpstoxSymbol("NSE:HDFCBANK")).toBe("NSE_EQ|INE040A01034");
    expect(toUpstoxSymbol("NSE:ICICIBANK")).toBe("NSE_EQ|INE090A01021");
    expect(toUpstoxSymbol("NSE:KOTAKBANK")).toBe("NSE_EQ|INE237A01028");
    expect(toUpstoxSymbol("NSE:AXISBANK")).toBe("NSE_EQ|INE238A01034");
    expect(toUpstoxSymbol("NSE:LT")).toBe("NSE_EQ|INE018A01030");
    expect(toUpstoxSymbol("NSE:WIPRO")).toBe("NSE_EQ|INE075A01022");
  });

  it("maps unknown equity to NSE_EQ|SYMBOL (fallback for unlisted symbols)", () => {
    expect(toUpstoxSymbol("NSE:UNLISTED")).toBe("NSE_EQ|UNLISTED");
  });

  it("maps NSE index to full-name V2 instrument key", () => {
    expect(toUpstoxSymbol("NSE:NIFTY50")).toBe("NSE_INDEX|Nifty 50");
    expect(toUpstoxSymbol("NSE:BANKNIFTY")).toBe("NSE_INDEX|Nifty Bank");
    expect(toUpstoxSymbol("NSE:FINNIFTY")).toBe("NSE_INDEX|Nifty Fin Service");
  });

  it("throws on malformed symbol", () => {
    expect(() => toUpstoxSymbol("INVALID")).toThrow();
    expect(() => toUpstoxSymbol("A:B:C")).toThrow();
  });
});
