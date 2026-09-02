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
  });

  it("maps unknown equity to NSE_EQ|SYMBOL", () => {
    expect(toUpstoxSymbol("NSE:TCS")).toBe("NSE_EQ|TCS");
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
