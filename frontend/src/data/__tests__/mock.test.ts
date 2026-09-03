import { describe, it, expect } from "vitest";
import { mockAIAnalysis, mockQuote } from "../mock";

describe("mockAIAnalysis", () => {
  it("includes priceDelta and priceDeltaPercent", () => {
    const ai = mockAIAnalysis("NSE:SBIN");
    expect(ai.priceDelta).toBeDefined();
    expect(ai.priceDeltaPercent).toBeDefined();
  });

  it("priceDeltaPercent is computed from priceDelta and decisionPrice", () => {
    const ai = mockAIAnalysis("NSE:SBIN");
    expect(ai.decisionPrice).toBeDefined();
    expect(ai.priceDelta).toBeDefined();
    expect(ai.priceDeltaPercent).toBeDefined();
    if (ai.decisionPrice > 0) {
      // Both values are rounded to 2 decimal places in the mock; use a
      // loose tolerance that accounts for rounding.
      expect(ai.priceDeltaPercent).toBeCloseTo(
        (ai.priceDelta / ai.decisionPrice) * 100,
        1,
      );
    }
  });

  it("includes decision_snapshot traceability fields", () => {
    const ai = mockAIAnalysis("NSE:NIFTY50");
    expect(ai.decisionPrice).toBeDefined();
    expect(ai.decisionTimestamp).toBeDefined();
    expect(ai.marketTimestamp).toBeDefined();
    expect(ai.dataFreshnessMs).toBeDefined();
    // Decision timestamp must be >= market timestamp (decision is on a later bar)
    expect(ai.decisionTimestamp).toBeGreaterThanOrEqual(ai.marketTimestamp);
  });

  it("produces deterministic output for the same symbol", () => {
    const a = mockAIAnalysis("NSE:RELIANCE");
    const b = mockAIAnalysis("NSE:RELIANCE");
    expect(a.bias).toBe(b.bias);
    expect(a.confidence).toBe(b.confidence);
    expect(a.signal).toBe(b.signal);
  });
});

describe("mockQuote", () => {
  it("includes marketTimestamp, lastUpdate, and fetchedAt", () => {
    const q = mockQuote("NSE:SBIN");
    expect(q.marketTimestamp).toBeDefined();
    expect(q.lastUpdate).toBeDefined();
    expect(q.fetchedAt).toBeDefined();
    // lastUpdate should equal marketTimestamp (backward compat alias)
    expect(q.lastUpdate).toBe(q.marketTimestamp);
    // fresh data should be recent
    expect(q.fetchedAt).toBeGreaterThan(Date.now() - 5_000);
  });

  it("price is always a finite number", () => {
    const q = mockQuote("NSE:TCS");
    expect(Number.isFinite(q.price)).toBe(true);
  });
});
