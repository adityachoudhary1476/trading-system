import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Mock the live data source so we can verify which API the Sidebar
// actually uses for production watchlist prices.
const getQuote = vi.fn();
const getOHLCV = vi.fn();
vi.mock("@/data/MarketDataSource", () => ({
  dataSource: {
    mode: "live",
    getQuote,
    getOHLCV,
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

vi.mock("@/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({ user: null, loading: false, signIn: vi.fn(), signOut: vi.fn() }),
}));

const Sidebar = (await import("../Sidebar")).Sidebar;

function readSource(relPath: string): string {
  return readFileSync(join(process.cwd(), relPath), "utf8");
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default to a successful quote; individual tests override.
  getQuote.mockResolvedValue({
    symbol: "NSE:NIFTY50",
    providerSymbol: "NSE:NIFTY50-INDEX",
    name: "NIFTY 50",
    exchange: "NSE",
    instrumentType: "index",
    price: 24842.15,
    previousClose: 24700,
    change: 142.15,
    changePct: 0.57,
    dayOpen: 24710,
    dayHigh: 24890,
    dayLow: 24680,
    volume: 12345678,
    vwap: undefined,
    dayRange: "24,680 — 24,890",
    volatility: undefined,
    sessionState: "REGULAR",
    lastUpdate: Date.now(),
  });
  getOHLCV.mockResolvedValue([
    { time: 1, open: 1, high: 2, low: 0.5, close: 1.5, volume: 100 },
    { time: 2, open: 1.5, high: 3, low: 1, close: 2, volume: 150 },
  ]);
});

describe("Sidebar — production watchlist data source", () => {
  it("Sidebar source does not import mockQuote()", () => {
    const src = readSource("src/components/layout/Sidebar.tsx");
    // mockQuote must not be used for production display.
    expect(src).not.toMatch(/mockQuote\(/);
  });

  it("fetches a real quote per watchlist symbol via dataSource.getQuote", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(getQuote).toHaveBeenCalled();
    });
    // Each of the watchlist symbols must have triggered exactly one quote fetch.
    const calledSymbols = getQuote.mock.calls.map((c) => c[0] as string);
    for (const sym of ["NSE:NIFTY50", "NSE:BANKNIFTY", "NSE:SBIN", "NSE:RELIANCE", "NSE:TCS", "NSE:INFY"]) {
      expect(calledSymbols).toContain(sym);
    }
  });

  it("renders a real price for each watchlist row when the quote resolves", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      // NIFTY 50 should display the live-formatted price.
      const niftyRow = screen.getAllByText("NIFTY50")[0]?.closest("button");
      expect(niftyRow?.textContent).toMatch(/24,?842/);
    });
  });

  it("renders — (em-dash) when the quote request fails (no fabricated value)", async () => {
    getQuote.mockRejectedValue(new Error("API request failed: 500"));
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      // At least one watchlist row should show an em-dash for the price.
      const dashes = screen.getAllByText("—");
      expect(dashes.length).toBeGreaterThan(0);
    });
  });

  it("renders a valid genuine zero changePct (does not coerce to em-dash)", async () => {
    getQuote.mockResolvedValue({
      symbol: "NSE:SBIN",
      providerSymbol: "NSE:SBIN-EQ",
      name: "State Bank of India",
      exchange: "NSE",
      instrumentType: "equity",
      price: 1000,
      previousClose: 1000,
      change: 0,
      changePct: 0,
      dayOpen: undefined,
      dayHigh: undefined,
      dayLow: undefined,
      volume: undefined,
      vwap: undefined,
      dayRange: "—",
      volatility: undefined,
      sessionState: "REGULAR",
      lastUpdate: Date.now(),
    });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      const sbin = screen.getAllByText("SBIN")[0]?.closest("button");
      expect(sbin?.textContent).toMatch(/\+0\.00%/);
    });
  });

  it("renders em-dash for changePct when the API omitted it (no fabrication)", async () => {
    getQuote.mockResolvedValue({
      symbol: "NSE:SBIN",
      providerSymbol: "NSE:SBIN-EQ",
      name: "State Bank of India",
      exchange: "NSE",
      instrumentType: "equity",
      price: 1000,
      previousClose: undefined,
      change: undefined,
      changePct: undefined,
      dayOpen: undefined,
      dayHigh: undefined,
      dayLow: undefined,
      volume: undefined,
      vwap: undefined,
      dayRange: "—",
      volatility: undefined,
      sessionState: "REGULAR",
      lastUpdate: Date.now(),
    });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      const sbin = screen.getAllByText("SBIN")[0]?.closest("button");
      // The change-pill text must be exactly "—" (em-dash), not "0.00%".
      expect(sbin?.textContent).toMatch(/—/);
      expect(sbin?.textContent).not.toMatch(/0\.00%/);
    });
  });

  it("does not duplicate quote requests for the same symbol", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(getQuote).toHaveBeenCalled();
    });
    // The parent-level cache should issue exactly one fetch per symbol.
    const calledSymbols = getQuote.mock.calls.map((c) => c[0] as string);
    const uniqueSymbols = new Set(calledSymbols);
    expect(calledSymbols.length).toBe(uniqueSymbols.size);
  });
});
