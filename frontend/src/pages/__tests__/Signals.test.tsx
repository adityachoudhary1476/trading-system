import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// --- Mock the data source so the page doesn't hit the network ---
vi.mock("@/data/MarketDataSource", () => ({
  dataSource: {
    mode: "live",
    getQuote: vi.fn(),
    getOHLCV: vi.fn(),
    getAIAnalysis: vi.fn(),
    getSignals: vi.fn(),
    getFeedHealth: vi.fn(),
    getPipeline: vi.fn(),
  },
}));

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => ({
    auth: { getSession: () => ({ data: { session: null }, error: null }) },
  }),
}));

vi.mock("@/store/AppContext", () => ({
  AppProvider: ({ children }: { children: React.ReactNode }) => children,
  useApp: () => ({
    selectedSymbol: "NSE:NIFTY50",
    setSelectedSymbol: vi.fn(),
    env: { mode: "live", environment: "production", dataSource: "API", execution: "DISABLED" },
  }),
  useIndianClock: () => new Date(),
}));

const { dataSource } = await import("@/data/MarketDataSource");
const ds = dataSource as unknown as {
  getSignals: ReturnType<typeof vi.fn>;
};

const renderSignals = async () => {
  const { SignalsPage } = await import("@/pages/Signals");
  return render(
    <MemoryRouter>
      <SignalsPage />
    </MemoryRouter>,
  );
};

const realSignal = (overrides: Partial<{
  id: string;
  symbol: string;
  direction: "long" | "short" | "hold" | "no_signal";
  confidence: number;
  price: number;
  bias: "bullish" | "bearish" | "neutral" | "choppy";
  reason: string;
  generatedAt: number;
  source: string;
}> = {}) => ({
  id: "sig-1",
  symbol: "NSE:SBIN",
  direction: "long" as const,
  confidence: 0.8,
  price: 850.5,
  bias: "bullish" as const,
  reason: "price>SMA20 + MACD>signal",
  generatedAt: Date.UTC(2024, 0, 5, 0, 0, 0), // 2024-01-05T00:00:00Z
  source: "deterministic",
  ...overrides,
});

describe("SignalsPage — price and timestamp rendering", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the real finite price (not ₹0.00) when price is provided", async () => {
    ds.getSignals.mockResolvedValue([realSignal({ price: 850.5, symbol: "NSE:SBIN" })]);
    const { container } = await renderSignals();
    await waitFor(() => {
      const rows = container.querySelectorAll("tbody tr");
      expect(rows.length).toBe(1);
    });
    // The price cell content must include "850" and ".50" (or "850.50" with grouping)
    const priceCell = Array.from(container.querySelectorAll("td.mono")).find(
      (td) => (td.textContent || "").includes("850"),
    );
    expect(priceCell).toBeTruthy();
    // Normalize whitespace and check we never see the fake "0.00" with ₹ prefix
    const allPriceCells = Array.from(container.querySelectorAll("td.mono")).filter(
      (td) => (td.textContent || "").startsWith("₹"),
    );
    expect(allPriceCells.length).toBe(1);
    expect(allPriceCells[0].textContent).not.toMatch(/0\.00/);
    expect(allPriceCells[0].textContent).toMatch(/850/);
  });

  it("renders the source-candle timestamp, not the wall clock", async () => {
    // 2024-01-05 00:00 UTC = 1704412800000
    const fixed = Date.UTC(2024, 0, 5, 0, 0, 0);
    ds.getSignals.mockResolvedValue([realSignal({ generatedAt: fixed })]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.querySelectorAll("tbody tr").length).toBe(1);
    });
    // The Time cell is the first td.mono in the row
    const timeCell = container.querySelector("tbody tr td.mono");
    expect(timeCell).not.toBeNull();
    const text = (timeCell!.textContent || "").replace(/\u00a0/g, " ").trim();
    expect(text).toMatch(/05/);
    expect(text).toMatch(/Jan/);
    // Must not be the wall-clock empty value "—"
    expect(text).not.toBe("—");
  });

  it("renders '—' for a non-finite or zero price (defensive)", async () => {
    ds.getSignals.mockResolvedValue([realSignal({ price: 0 })]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.querySelectorAll("tbody tr").length).toBe(1);
    });
    const priceCells = Array.from(container.querySelectorAll("td.mono")).filter(
      (td) => (td.textContent || "").startsWith("₹"),
    );
    expect(priceCells.length).toBe(1);
    expect(priceCells[0].textContent).toBe("₹—");
  });

  it("does not include the obsolete 'Mock signal history.' text", async () => {
    ds.getSignals.mockResolvedValue([realSignal()]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.querySelectorAll("tbody tr").length).toBe(1);
    });
    expect(container.innerHTML).not.toMatch(/Mock signal history/i);
  });

  it("renders the table with all required columns", async () => {
    ds.getSignals.mockResolvedValue([realSignal()]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.querySelectorAll("tbody tr").length).toBe(1);
    });
    const headers = Array.from(container.querySelectorAll("thead th")).map(
      (th) => (th.textContent || "").trim(),
    );
    expect(headers).toEqual(
      expect.arrayContaining(["Time", "Symbol", "Signal", "Confidence", "Price", "Bias", "Reason"]),
    );
  });

  it("shows empty state when there are no signals and no mock text", async () => {
    ds.getSignals.mockResolvedValue([]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.textContent).toMatch(/No signals match/i);
    });
    expect(container.innerHTML).not.toMatch(/Mock signal history/i);
  });

  it("renders bias and reason text from the real signal", async () => {
    ds.getSignals.mockResolvedValue([
      realSignal({
        bias: "bearish",
        reason: "price<SMA20 + MACD<signal",
        direction: "short",
      }),
    ]);
    const { container } = await renderSignals();
    await waitFor(() => {
      expect(container.textContent).toContain("bearish");
      expect(container.textContent).toContain("price<SMA20 + MACD<signal");
    });
  });
});
