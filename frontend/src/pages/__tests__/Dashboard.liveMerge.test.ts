import { describe, it, expect, vi, beforeAll } from "vitest";
import type { OHLCVBar } from "@/types";

vi.mock("@/data/MarketDataSource", () => ({
  dataSource: {
    mode: "mock" as const,
    getQuote: vi.fn(),
    getOHLCV: vi.fn(),
    getAIAnalysis: vi.fn(),
    getSignals: vi.fn(),
    getFeedHealth: vi.fn(),
    getPipeline: vi.fn(),
    getMarketStatus: vi.fn(),
  },
}));
vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => ({
    auth: { getSession: () => ({ data: { session: null } }) },
  }),
}));
vi.mock("@/store/AppContext", () => ({
  useApp: () => ({
    selectedSymbol: "NSE:NIFTY50",
    setSelectedSymbol: vi.fn(),
    env: { mode: "live", environment: "production", dataSource: "API", execution: "DISABLED" },
  }),
}));

let mergeLiveTick: (bars: OHLCVBar[] | null, price: number, ts: number, barMs: number) => OHLCVBar[] | null;
let floorToBar: (ts: number, barMs: number) => number;

beforeAll(async () => {
  const mod = await import("@/pages/Dashboard");
  mergeLiveTick = mod.mergeLiveTick;
  floorToBar = mod.floorToBar;
});

// --- IST timezone constants ------------------------------------------------
// NSE candles are timestamped at 00:00 IST (= 18:30 UTC the previous calendar
// day). These helpers build epoch-ms timestamps in that convention.

/** 00:00 IST on the given UTC date → epoch ms. */
function istMidnight(year: number, month: number, day: number): number {
  // 00:00 IST = 18:30 UTC of the previous UTC day
  return Date.UTC(year, month - 1, day, 0, 0, 0, 0) - 5.5 * 60 * 60_000;
}

const DAY_MS = 24 * 60 * 60_000;
const MINUTE_MS = 60_000;

function bar(time: number, open: number, high: number, low: number, close: number, volume = 0): OHLCVBar {
  return { time, open, high, low, close, volume };
}

// --- floorToBar ------------------------------------------------------------

describe("floorToBar — IST alignment", () => {
  it("maps a daily candle and a same-day live tick to the same bar boundary", () => {
    // Jan 3 2024 NSE daily candle: 00:00 IST = Jan 2 18:30 UTC
    const candleTime = istMidnight(2024, 1, 3);
    // Live tick at Jan 3 10:00 AM IST = Jan 3 04:30 UTC
    const tickTime = candleTime + 10 * 60 * 60_000;

    expect(floorToBar(candleTime, DAY_MS)).toBe(floorToBar(tickTime, DAY_MS));
  });

  it("places a late-session tick in the same bar as the candle", () => {
    // Jan 3 00:00 IST candle
    const candleTime = istMidnight(2024, 1, 3);
    // Tick at Jan 3 3:29 PM IST (just before NSE close) = Jan 3 09:59 UTC
    const tickTime = candleTime + 15 * 60 * 60_000 + 29 * 60_000;

    expect(floorToBar(candleTime, DAY_MS)).toBe(floorToBar(tickTime, DAY_MS));
  });

  it("places a tick on the next trading day in a different bar", () => {
    const jan3 = istMidnight(2024, 1, 3);
    const jan4 = istMidnight(2024, 1, 4);
    const tickTime = jan4 + 10 * 60 * 60_000; // Jan 4 10:00 AM IST

    expect(floorToBar(jan3, DAY_MS)).not.toBe(floorToBar(tickTime, DAY_MS));
    expect(floorToBar(tickTime, DAY_MS)).toBe(floorToBar(jan4, DAY_MS));
  });
});

// --- mergeLiveTick ---------------------------------------------------------

describe("mergeLiveTick — live tick merge into daily bars", () => {
  it("updates the last bar when the tick is within the same trading day (IST)", () => {
    // Two daily candles: Jan 2 and Jan 3 (IST 00:00).
    const bars: OHLCVBar[] = [
      bar(istMidnight(2024, 1, 2), 100, 105, 98, 103, 50_000),
      bar(istMidnight(2024, 1, 3), 103, 108, 102, 106, 45_000),
    ];
    // Live tick at Jan 3 10:00 AM IST — same trading day as the last candle
    const tickTs = istMidnight(2024, 1, 3) + 10 * 60 * 60_000;
    const price = 109.5;

    const result = mergeLiveTick(bars, price, tickTs, DAY_MS);

    // Must NOT append — should still be 2 bars
    expect(result).toHaveLength(2);
    // Last bar should have updated close, high extended
    expect(result![1].close).toBe(109.5);
    expect(result![1].high).toBe(109.5);
    expect(result![1].low).toBe(102);
    expect(result![1].volume).toBe(45_000);
  });

  it("appends a new bar when the tick is in a later trading day", () => {
    const bars: OHLCVBar[] = [
      bar(istMidnight(2024, 1, 2), 100, 105, 98, 103, 50_000),
      bar(istMidnight(2024, 1, 3), 103, 108, 102, 106, 45_000),
    ];
    // Tick at Jan 4 10:00 AM IST — a new trading day
    const tickTs = istMidnight(2024, 1, 4) + 10 * 60 * 60_000;
    const price = 110;

    const result = mergeLiveTick(bars, price, tickTs, DAY_MS);

    expect(result).toHaveLength(3);
    const newBar = result![2];
    expect(newBar.time).toBe(floorToBar(tickTs, DAY_MS));
    expect(newBar.open).toBe(110);
    expect(newBar.high).toBe(110);
    expect(newBar.low).toBe(110);
    expect(newBar.close).toBe(110);
  });

  it("returns bars unchanged when the tick falls before the last candle", () => {
    const bars: OHLCVBar[] = [
      bar(istMidnight(2024, 1, 2), 100, 105, 98, 103, 50_000),
      bar(istMidnight(2024, 1, 3), 103, 108, 102, 106, 45_000),
    ];
    // Tick at Jan 2 10:00 AM IST — before the last candle (Jan 3)
    const tickTs = istMidnight(2024, 1, 2) + 10 * 60 * 60_000;

    const result = mergeLiveTick(bars, 109, tickTs, DAY_MS);
    expect(result).toBe(bars);
  });

  it("updates low when the tick price is below the bar's low", () => {
    const bars: OHLCVBar[] = [
      bar(istMidnight(2024, 1, 3), 106, 108, 102, 106, 45_000),
    ];
    const tickTs = istMidnight(2024, 1, 3) + 10 * 60 * 60_000;

    const result = mergeLiveTick(bars, 99, tickTs, DAY_MS);

    expect(result![0].low).toBe(99);
    expect(result![0].high).toBe(108);
    expect(result![0].close).toBe(99);
  });
});

// --- mergeLiveTick edge cases ----------------------------------------------

describe("mergeLiveTick — edge cases", () => {
  it("returns null when bars is null", () => {
    expect(mergeLiveTick(null, 100, Date.now(), DAY_MS)).toBeNull();
  });

  it("returns bars unchanged when price is 0", () => {
    const bars: OHLCVBar[] = [bar(Date.now(), 100, 105, 98, 103, 1000)];
    expect(mergeLiveTick(bars, 0, Date.now(), DAY_MS)).toBe(bars);
  });

  it("returns bars unchanged when price is negative", () => {
    const bars: OHLCVBar[] = [bar(Date.now(), 100, 105, 98, 103, 1000)];
    expect(mergeLiveTick(bars, -1, Date.now(), DAY_MS)).toBe(bars);
  });

  it("returns bars unchanged when price is NaN", () => {
    const bars: OHLCVBar[] = [bar(Date.now(), 100, 105, 98, 103, 1000)];
    expect(mergeLiveTick(bars, NaN, Date.now(), DAY_MS)).toBe(bars);
  });

  it("returns bars unchanged when barMs is 0 (unsupported timeframe)", () => {
    const bars: OHLCVBar[] = [bar(Date.now(), 100, 105, 98, 103, 1000)];
    expect(mergeLiveTick(bars, 109, Date.now(), 0)).toBe(bars);
  });

  it("returns bars unchanged when ts is invalid", () => {
    const bars: OHLCVBar[] = [bar(Date.now(), 100, 105, 98, 103, 1000)];
    expect(mergeLiveTick(bars, 109, 0, DAY_MS)).toBe(bars);
    expect(mergeLiveTick(bars, 109, NaN, DAY_MS)).toBe(bars);
  });

  it("returns bars unchanged when bars array is empty", () => {
    expect(mergeLiveTick([], 109, Date.now(), DAY_MS)).toEqual([]);
  });
});

// --- mergeLiveTick with 1-minute bars --------------------------------------

describe("mergeLiveTick — 1-minute bars", () => {
  it("merges a tick within the same minute into the last 1m bar", () => {
    // 1m bar at 9:15:00 AM IST (epoch ms, already UTC).
    const barTime = Date.UTC(2024, 0, 3, 3, 45, 0, 0); // 9:15 IST
    const bars: OHLCVBar[] = [bar(barTime, 100, 102, 99, 101, 1000)];
    // Tick at 9:15:30 AM IST
    const tickTs = barTime + 30_000;

    const result = mergeLiveTick(bars, 103, tickTs, MINUTE_MS);

    expect(result).toHaveLength(1);
    expect(result![0].close).toBe(103);
    expect(result![0].high).toBe(103);
  });

  it("appends a new bar when the tick crosses into the next minute", () => {
    const barTime = Date.UTC(2024, 0, 3, 3, 45, 0, 0); // 9:15 IST
    const bars: OHLCVBar[] = [bar(barTime, 100, 102, 99, 101, 1000)];
    // Tick at 9:16:00 AM IST
    const tickTs = barTime + 60_000;

    const result = mergeLiveTick(bars, 103, tickTs, MINUTE_MS);

    expect(result).toHaveLength(2);
    expect(result![1].open).toBe(103);
    expect(result![1].close).toBe(103);
  });
});
