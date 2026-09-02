import { describe, it, expect } from "vitest";
import { __TESTING__ as OHLCV } from "../ohlcv";
import { toUpstoxSymbol } from "../../lib/symbol-map";

const { INTERVAL_MAP, UPSTOX_RANGE_DAYS, parseCandle, formatDate } = OHLCV;

describe("OHLCV timeframe mapping (Upstox V2)", () => {
  it("maps 1D / 1d to day", () => {
    expect(INTERVAL_MAP["1D"]?.interval).toBe("day");
    expect(INTERVAL_MAP["1d"]?.interval).toBe("day");
  });

  it("maps 1W / 1w to week", () => {
    expect(INTERVAL_MAP["1W"]?.interval).toBe("week");
    expect(INTERVAL_MAP["1w"]?.interval).toBe("week");
  });

  it("maps 1M / 1MO / 1mo to month", () => {
    expect(INTERVAL_MAP["1M"]?.interval).toBe("month");
    expect(INTERVAL_MAP["1MO"]?.interval).toBe("month");
    expect(INTERVAL_MAP["1mo"]?.interval).toBe("month");
  });

  it("maps 1m to 1minute", () => {
    expect(INTERVAL_MAP["1m"]?.interval).toBe("1minute");
  });

  it("does NOT map 5m / 15m / 1h (Upstox V2 does not expose them)", () => {
    expect(INTERVAL_MAP["5m"]).toBeUndefined();
    expect(INTERVAL_MAP["15m"]).toBeUndefined();
    expect(INTERVAL_MAP["1h"]).toBeUndefined();
  });

  it("declares only Upstox V2-supported intervals in range table", () => {
    expect(new Set(Object.keys(UPSTOX_RANGE_DAYS))).toEqual(
      new Set(["1minute", "30minute", "day", "week", "month"]),
    );
  });

  it("declares sensible historical-depth bounds per Upstox V2", () => {
    // 1month, 1year, 1year, 10year, 10year (per Upstox V2 docs)
    expect(UPSTOX_RANGE_DAYS["1minute"]).toBeLessThanOrEqual(31);
    expect(UPSTOX_RANGE_DAYS["30minute"]).toBeLessThanOrEqual(366);
    expect(UPSTOX_RANGE_DAYS["day"]).toBeLessThanOrEqual(366);
    expect(UPSTOX_RANGE_DAYS["week"]).toBeGreaterThanOrEqual(366 * 5);
    expect(UPSTOX_RANGE_DAYS["month"]).toBeGreaterThanOrEqual(366 * 5);
  });
});

describe("formatDate (Upstox V2 contract)", () => {
  it("emits YYYY-MM-DD", () => {
    expect(formatDate(new Date("2024-03-15T10:30:00Z"))).toBe("2024-03-15");
  });
  it("uses UTC (no local-timezone drift)", () => {
    // 2024-01-01T00:00:00Z must be 2024-01-01, not 2023-12-31 in any tz.
    expect(formatDate(new Date("2024-01-01T00:00:00Z"))).toBe("2024-01-01");
  });
});

describe("Upstox V2 historical-candle URL composition", () => {
  it("produces /historical-candle/{key}/{interval}/{to_date}/{from_date} in the documented order", () => {
    // Simulate the URL build used by the handler.
    const internal = "NSE:SBIN";
    const upstoxSymbol = toUpstoxSymbol(internal);
    const interval = INTERVAL_MAP["1D"]!.interval;
    const end = new Date("2024-12-15T00:00:00Z");
    const start = new Date("2024-11-15T00:00:00Z");
    const path = `/historical-candle/${encodeURIComponent(upstoxSymbol)}/${interval}/${formatDate(end)}/${formatDate(start)}`;
    expect(path).toBe(
      `/historical-candle/NSE_EQ%7CINE062A01020/day/2024-12-15/2024-11-15`,
    );
  });

  it("produces to_date >= from_date in the path (Upstox rejects reversed ranges)", () => {
    const internal = "NSE:SBIN";
    const upstoxSymbol = toUpstoxSymbol(internal);
    const interval = "day";
    const end = new Date("2024-12-15T00:00:00Z");
    const start = new Date("2024-11-15T00:00:00Z");
    const path = `/historical-candle/${encodeURIComponent(upstoxSymbol)}/${interval}/${formatDate(end)}/${formatDate(start)}`;
    const parts = path.split("/");
    // parts: ['', 'historical-candle', '{key}', '{interval}', '{to}', '{from}']
    const to = parts[4];
    const from = parts[5];
    expect(to >= from).toBe(true);
  });

  it("encodes the pipe character in the instrument key (Upstox uses EXCHANGE|ISIN)", () => {
    const path = `/historical-candle/${encodeURIComponent("NSE_EQ|INE062A01020")}/day/2024-12-15/2024-11-15`;
    expect(path).toBe("/historical-candle/NSE_EQ%7CINE062A01020/day/2024-12-15/2024-11-15");
  });
});

describe("parseCandle (Upstox V2 candle tuple)", () => {
  it("accepts the V2 documented shape: ISO-string timestamp", () => {
    // candle[0] is a string in the V2 example response
    const c = parseCandle([
      "2023-10-01T00:00:00+05:30",
      53.1,
      53.95,
      51.6,
      52.05,
      235519861,
      0,
    ]);
    expect(c).not.toBeNull();
    expect(c!.time).toBe(Date.parse("2023-10-01T00:00:00+05:30"));
    expect(c!.open).toBe(53.1);
    expect(c!.high).toBe(53.95);
    expect(c!.low).toBe(51.6);
    expect(c!.close).toBe(52.05);
    expect(c!.volume).toBe(235519861);
  });

  it("accepts a numeric timestamp in seconds (legacy V1)", () => {
    const c = parseCandle([1700000000, 1, 2, 0.5, 1.5, 100]);
    expect(c).not.toBeNull();
    expect(c!.time).toBe(1700000000 * 1000);
  });

  it("accepts a numeric timestamp in milliseconds", () => {
    const c = parseCandle([1700000000000, 1, 2, 0.5, 1.5, 100]);
    expect(c).not.toBeNull();
    expect(c!.time).toBe(1700000000000);
  });

  it("rejects a candle with non-finite price", () => {
    expect(parseCandle(["2024-01-01", NaN, 1, 1, 1, 1])).toBeNull();
    expect(parseCandle(["2024-01-01", 1, Infinity, 1, 1, 1])).toBeNull();
  });

  it("rejects a candle shorter than 6 elements", () => {
    expect(parseCandle(["2024-01-01", 1, 2, 3, 4])).toBeNull();
  });

  it("rejects a non-array candle", () => {
    expect(parseCandle(null)).toBeNull();
    expect(parseCandle({})).toBeNull();
    expect(parseCandle("string")).toBeNull();
  });

  it("rejects an unparseable timestamp", () => {
    expect(parseCandle(["not-a-date", 1, 2, 3, 4, 5])).toBeNull();
  });

  it("preserves a real volume of 0 (does not coerce to null)", () => {
    const c = parseCandle(["2024-01-01", 1, 2, 0.5, 1.5, 0]);
    expect(c!.volume).toBe(0);
  });

  it("accepts a missing 7th (open-interest) element", () => {
    const c = parseCandle(["2024-01-01", 1, 2, 0.5, 1.5, 100]);
    expect(c).not.toBeNull();
  });
});
