import { describe, it, expect } from "vitest";
import { fmtNum, fmtPrice, fmtSigned, fmtPct, fmtVolume, fmtDuration, isFiniteNumber } from "../format";

describe("isFiniteNumber", () => {
  it("returns true for valid finite numbers including zero", () => {
    expect(isFiniteNumber(0)).toBe(true);
    expect(isFiniteNumber(1)).toBe(true);
    expect(isFiniteNumber(-1)).toBe(true);
    expect(isFiniteNumber(24842.15)).toBe(true);
  });
  it("returns false for undefined", () => expect(isFiniteNumber(undefined)).toBe(false));
  it("returns false for null", () => expect(isFiniteNumber(null)).toBe(false));
  it("returns false for NaN", () => expect(isFiniteNumber(NaN)).toBe(false));
  it("returns false for Infinity", () => expect(isFiniteNumber(Infinity)).toBe(false));
  it("returns false for -Infinity", () => expect(isFiniteNumber(-Infinity)).toBe(false));
  it("returns false for strings", () => expect(isFiniteNumber("123")).toBe(false));
});

describe("fmtNum", () => {
  it("returns '—' for undefined", () => expect(fmtNum(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtNum(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtNum(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtNum(Infinity)).toBe("—"));
  it("returns '—' for -Infinity", () => expect(fmtNum(-Infinity)).toBe("—"));
  it("formats zero as '0'", () => expect(fmtNum(0)).toBe("0"));
  it("formats zero with useGrouping as '0'", () => expect(fmtNum(0, { useGrouping: true })).toBe("0"));
  it("formats zero with fraction digits as '0.00'", () =>
    expect(fmtNum(0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })).toBe("0.00"));
  it("formats positive numbers", () => expect(fmtNum(1000)).toBe("1,000"));
  it("formats negative numbers", () => expect(fmtNum(-1000)).toBe("-1,000"));
});

describe("fmtPrice", () => {
  it("returns '—' for undefined", () => expect(fmtPrice(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtPrice(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtPrice(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtPrice(Infinity)).toBe("—"));
  it("formats zero as '0.00'", () => expect(fmtPrice(0)).toBe("0.00"));
  it("formats positive price", () => expect(fmtPrice(24842.15)).toBe("24,842.15"));
  it("formats negative price", () => expect(fmtPrice(-184.2)).toBe("-184.20"));
});

describe("fmtSigned", () => {
  it("returns '—' for undefined", () => expect(fmtSigned(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtSigned(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtSigned(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtSigned(Infinity)).toBe("—"));
  it("returns '—' for -Infinity", () => expect(fmtSigned(-Infinity)).toBe("—"));
  it("formats zero as '+0.00'", () => expect(fmtSigned(0)).toBe("+0.00"));
  it("formats positive number with + sign", () => expect(fmtSigned(184.2)).toBe("+184.20"));
  it("formats negative number with - sign", () => expect(fmtSigned(-12.4)).toBe("-12.40"));
});

describe("fmtPct", () => {
  it("returns '—' for undefined", () => expect(fmtPct(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtPct(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtPct(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtPct(Infinity)).toBe("—"));
  it("returns '—' for -Infinity", () => expect(fmtPct(-Infinity)).toBe("—"));
  it("formats zero as '+0.00%'", () => expect(fmtPct(0)).toBe("+0.00%"));
  it("formats positive percentage", () => expect(fmtPct(1.25)).toBe("+1.25%"));
  it("formats negative percentage", () => expect(fmtPct(-0.5)).toBe("-0.50%"));
});

describe("fmtVolume", () => {
  it("returns '—' for undefined", () => expect(fmtVolume(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtVolume(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtVolume(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtVolume(Infinity)).toBe("—"));
  it("returns '—' for -Infinity", () => expect(fmtVolume(-Infinity)).toBe("—"));
  it("formats zero as '0'", () => expect(fmtVolume(0)).toBe("0"));
  it("formats small volume", () => expect(fmtVolume(1500000)).toBe("15.00 L"));
  it("formats lakh volume", () => expect(fmtVolume(8400000)).toBe("84.00 L"));
  it("formats crore volume", () => expect(fmtVolume(120000000)).toBe("12.00 Cr"));
});

describe("fmtDuration", () => {
  it("returns '—' for undefined", () => expect(fmtDuration(undefined)).toBe("—"));
  it("returns '—' for null", () => expect(fmtDuration(null)).toBe("—"));
  it("returns '—' for NaN", () => expect(fmtDuration(NaN)).toBe("—"));
  it("returns '—' for Infinity", () => expect(fmtDuration(Infinity)).toBe("—"));
  it("returns '—' for -Infinity", () => expect(fmtDuration(-Infinity)).toBe("—"));
  it("returns '—' for negative durations", () => expect(fmtDuration(-5000)).toBe("—"));
  it("formats zero as '0.0s'", () => expect(fmtDuration(0)).toBe("0.0s"));
  it("formats sub-minute durations in seconds", () => expect(fmtDuration(5_000)).toBe("5.0s"));
  it("formats exactly 60s as minutes", () => expect(fmtDuration(60_000)).toBe("1.0m"));
  it("formats sub-hour durations in minutes", () => expect(fmtDuration(300_000)).toBe("5.0m"));
  it("formats hour-and-above durations in hours", () => expect(fmtDuration(86_400_000)).toBe("24.0h"));
});
