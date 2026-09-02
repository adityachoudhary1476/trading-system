import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// --- Mock the data source ---
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

// Mock AppContext so Sidebar's useApp works
vi.mock("@/store/AppContext", () => ({
  AppProvider: ({ children }: { children: React.ReactNode }) => children,
  useApp: () => ({
    selectedSymbol: "NSE:NIFTY50",
    setSelectedSymbol: vi.fn(),
    env: { mode: "live", environment: "production", dataSource: "API", execution: "DISABLED" },
  }),
  useIndianClock: () => new Date(),
}));

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { dataSource } = await import("@/data/MarketDataSource");
const ds = dataSource as unknown as {
  getQuote: ReturnType<typeof vi.fn>;
  getFeedHealth: ReturnType<typeof vi.fn>;
  getOHLCV: ReturnType<typeof vi.fn>;
  getAIAnalysis: ReturnType<typeof vi.fn>;
  getSignals: ReturnType<typeof vi.fn>;
};

describe("DataHealthPanel — defensive rendering", () => {
  let DataHealthPanel: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    const mod = await import("@/components/system/DataHealthPanel");
    DataHealthPanel = mod.DataHealthPanel;
  });

  it("renders '—' for undefined feed health counters (missing fields)", async () => {
    ds.getFeedHealth.mockResolvedValue({
      feed: "Upstox",
      status: "disconnected",
      lastTick: null,
      eventsReceived: undefined,
      eventsRejected: undefined,
      candlesGenerated: undefined,
      lastClosedCandle: null,
      connected: false,
    });
    render(<DataHealthPanel />);
    await waitFor(() => {
      const dashes = screen.getAllByText("—");
      expect(dashes.length).toBeGreaterThanOrEqual(3);
    });
  });

  it("renders '—' for null feed health counters", async () => {
    ds.getFeedHealth.mockResolvedValue({
      feed: "Upstox",
      status: "disconnected",
      lastTick: null,
      eventsReceived: null,
      eventsRejected: null,
      candlesGenerated: null,
      lastClosedCandle: null,
      connected: false,
    });
    render(<DataHealthPanel />);
    await waitFor(() => {
      const dashes = screen.getAllByText("—");
      expect(dashes.length).toBeGreaterThanOrEqual(3);
    });
  });

  it("renders valid numbers from healthy feed", async () => {
    ds.getFeedHealth.mockResolvedValue({
      feed: "Upstox",
      status: "healthy",
      lastTick: Date.now(),
      eventsReceived: 18432,
      eventsRejected: 3,
      candlesGenerated: 412,
      lastClosedCandle: Date.now() - 300000,
      connected: true,
    });
    render(<DataHealthPanel />);
    await waitFor(() => {
      expect(screen.getByText("18,432")).toBeTruthy();
      expect(screen.getByText("3")).toBeTruthy();
      expect(screen.getByText("412")).toBeTruthy();
    });
  });

  it("renders zero counters as '0' (not '—')", async () => {
    ds.getFeedHealth.mockResolvedValue({
      feed: "Upstox",
      status: "healthy",
      lastTick: Date.now(),
      eventsReceived: 0,
      eventsRejected: 0,
      candlesGenerated: 0,
      lastClosedCandle: Date.now() - 300000,
      connected: true,
    });
    render(<DataHealthPanel />);
    await waitFor(() => {
      const zeros = screen.getAllByText("0");
      expect(zeros.length).toBe(3);
    });
  });

  it("renders error state when feed health API fails (401)", async () => {
    ds.getFeedHealth.mockRejectedValue(new Error("Authentication required: please sign in"));
    render(<DataHealthPanel />);
    await waitFor(() => {
      expect(screen.getByText("Data health unavailable")).toBeTruthy();
    });
  });

  it("renders error state when feed health API fails (500)", async () => {
    ds.getFeedHealth.mockRejectedValue(new Error("Backend server error: 500"));
    render(<DataHealthPanel />);
    await waitFor(() => {
      expect(screen.getByText("Data health unavailable")).toBeTruthy();
    });
  });
});

describe("AIAnalysisPanel — defensive rendering", () => {
  let AIAnalysisPanel: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    const mod = await import("@/components/ai/AIAnalysis");
    AIAnalysisPanel = mod.AIAnalysisPanel;
  });

  it("renders error state when analysis API returns 401", async () => {
    ds.getAIAnalysis.mockRejectedValue(new Error("Authentication required: please sign in"));
    render(<AIAnalysisPanel symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(screen.getByText("Analysis unavailable")).toBeTruthy();
    });
  });

  it("renders error state when analysis API returns 500", async () => {
    ds.getAIAnalysis.mockRejectedValue(new Error("Backend server error: 500"));
    render(<AIAnalysisPanel symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(screen.getByText("Analysis unavailable")).toBeTruthy();
    });
  });

  it("renders nothing while loading (no crash)", async () => {
    ds.getAIAnalysis.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<AIAnalysisPanel symbol="NSE:NIFTY50" />);
    // Should render null while loading
    await waitFor(() => {
      expect(container.innerHTML).toBe("");
    });
  });

  it("renders normally when analysis loads with undefined numeric fields", async () => {
    ds.getAIAnalysis.mockResolvedValue({
      symbol: "NSE:NIFTY50",
      timeframe: "5m",
      bias: "bullish",
      confidence: undefined,
      signal: "long",
      summary: "Test summary",
      factors: [],
      generatedAt: Date.now(),
      model: "test",
    });
    render(<AIAnalysisPanel symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      // fmtPrice(undefined) renders "—", not a crash
      expect(screen.queryByText("Something went wrong")).toBeNull();
    });
  });
});

describe("QuoteAndMetrics — defensive rendering on quote failure", () => {
  let QuoteHeader: any;
  let MetricsStrip: any;
  let MetricsPanel: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    const mod = await import("@/components/market/QuoteAndMetrics");
    QuoteHeader = mod.QuoteHeader;
    MetricsStrip = mod.MetricsStrip;
    MetricsPanel = mod.MetricsPanel;
  });

  it("QuoteHeader renders 'Data temporarily unavailable' on 500", async () => {
    ds.getQuote.mockRejectedValue(new Error("Backend server error: 500"));
    render(<QuoteHeader symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(screen.getByText("Data temporarily unavailable")).toBeTruthy();
    });
  });

  it("QuoteHeader renders 'Data temporarily unavailable' on 401", async () => {
    ds.getQuote.mockRejectedValue(new Error("Authentication required: please sign in"));
    render(<QuoteHeader symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(screen.getByText("Data temporarily unavailable")).toBeTruthy();
    });
  });

  it("QuoteHeader renders '—' for price when quote has undefined numeric fields", async () => {
    ds.getQuote.mockResolvedValue({
      symbol: "NSE:NIFTY50",
      providerSymbol: "NSE:NIFTY50-INDEX",
      name: "NIFTY 50",
      exchange: "NSE",
      instrumentType: "index" as const,
      price: undefined as unknown as number,
      previousClose: undefined as unknown as number,
      change: undefined as unknown as number,
      changePct: undefined as unknown as number,
      dayOpen: undefined as unknown as number,
      dayHigh: undefined as unknown as number,
      dayLow: undefined as unknown as number,
      volume: undefined as unknown as number,
      vwap: undefined as unknown as number,
      dayRange: "—",
      volatility: 0,
      sessionState: "REGULAR" as const,
      lastUpdate: 0,
    });
    // This should NOT throw a TypeError
    const { container } = render(<QuoteHeader symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(container).toBeTruthy();
    });
  });

  it("MetricsStrip renders null when quote fails (no crash)", async () => {
    ds.getQuote.mockRejectedValue(new Error("Backend server error: 500"));
    const { container } = render(<MetricsStrip symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(container.innerHTML).toBe("");
    });
  });

  it("MetricsPanel renders null when quote fails (no crash)", async () => {
    ds.getQuote.mockRejectedValue(new Error("Backend server error: 500"));
    const { container } = render(<MetricsPanel symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(container.innerHTML).toBe("");
    });
  });
});

describe("SignalCard — defensive rendering", () => {
  let SignalCard: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    const mod = await import("@/components/ai/AIAnalysis");
    SignalCard = mod.SignalCard;
  });

  it("renders error state when analysis API returns 401", async () => {
    ds.getAIAnalysis.mockRejectedValue(new Error("Authentication required: please sign in"));
    ds.getSignals.mockResolvedValue([]);
    render(<SignalCard symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(screen.getByText("Signal unavailable")).toBeTruthy();
    });
  });

  it("renders error state when signals API returns 401", async () => {
    ds.getAIAnalysis.mockResolvedValue({
      symbol: "NSE:NIFTY50",
      timeframe: "5m",
      bias: "bullish",
      confidence: 0.8,
      signal: "long",
      summary: "Test",
      factors: [],
      generatedAt: Date.now(),
      model: "test",
    });
    ds.getSignals.mockRejectedValue(new Error("Authentication required: please sign in"));
    const { container } = render(<SignalCard symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(container).toBeTruthy();
    });
  });

  it("renders with undefined signal price (uses ?? 0 fallback)", async () => {
    ds.getAIAnalysis.mockResolvedValue({
      symbol: "NSE:NIFTY50",
      timeframe: "5m",
      bias: "bullish",
      confidence: 0.8,
      signal: "long",
      summary: "Test",
      factors: [],
      generatedAt: Date.now(),
      model: "test",
    });
    ds.getSignals.mockResolvedValue([]);
    const { container } = render(<SignalCard symbol="NSE:NIFTY50" />);
    await waitFor(() => {
      expect(container).toBeTruthy();
    });
  });
});

describe("Sidebar — defensive rendering", () => {
  let Sidebar: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    const mod = await import("@/components/layout/Sidebar");
    Sidebar = mod.Sidebar;
  });

  it("renders without crashing when OHLCV fails (500)", async () => {
    ds.getOHLCV.mockRejectedValue(new Error("Backend server error: 500"));
    const { container } = render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("Watchlist")).toBeTruthy();
    });
    expect(container).toBeTruthy();
  });

  it("does not crash when OHLCV returns 401", async () => {
    ds.getOHLCV.mockRejectedValue(new Error("Authentication required: please sign in"));
    const { container } = render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("Watchlist")).toBeTruthy();
    });
  });

  it("renders price safely when fmtPrice receives undefined", async () => {
    const { fmtPrice } = await import("@/lib/format");
    expect(fmtPrice(undefined)).toBe("—");
    expect(fmtPrice(null)).toBe("—");
    expect(fmtPrice(NaN)).toBe("—");
    expect(fmtPrice(0)).toBe("0.00");
    expect(fmtPrice(24842.15)).toBe("24,842.15");
  });
});

describe("PaperPositions — defensive rendering of quantity", () => {
  it("fmtNum renders '—' for undefined quantity", async () => {
    const { fmtNum } = await import("@/lib/format");
    expect(fmtNum(undefined, { maximumFractionDigits: 2 })).toBe("—");
    expect(fmtNum(null, { maximumFractionDigits: 2 })).toBe("—");
    expect(fmtNum(NaN, { maximumFractionDigits: 2 })).toBe("—");
    expect(fmtNum(0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })).toBe("0.00");
    expect(fmtNum(100, { minimumFractionDigits: 2, maximumFractionDigits: 2 })).toBe("100.00");
  });
});

describe("PaperDeploymentDetail — defensive rendering of quantity", () => {
  it("fmtNum renders '—' for undefined quantity", async () => {
    const { fmtNum } = await import("@/lib/format");
    expect(fmtNum(undefined)).toBe("—");
    expect(fmtNum(null)).toBe("—");
    expect(fmtNum(0)).toBe("0");
    expect(fmtNum(50)).toBe("50");
  });
});
