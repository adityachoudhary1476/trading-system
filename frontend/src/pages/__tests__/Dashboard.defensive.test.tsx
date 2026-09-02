import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Mock all data source calls to simulate total API failure
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

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { dataSource } = await import("@/data/MarketDataSource");
const ds = dataSource as unknown as {
  getQuote: ReturnType<typeof vi.fn>;
  getOHLCV: ReturnType<typeof vi.fn>;
  getAIAnalysis: ReturnType<typeof vi.fn>;
  getSignals: ReturnType<typeof vi.fn>;
  getFeedHealth: ReturnType<typeof vi.fn>;
  getPipeline: ReturnType<typeof vi.fn>;
};

// Mock fetchConnectionStatus (used by SystemPage)
vi.mock("@/lib/upstox", () => ({
  fetchConnectionStatus: vi.fn().mockRejectedValue(new Error("network")),
  startUpstoxOAuth: vi.fn(),
  disconnectUpstox: vi.fn(),
}));

// AppProvider mock — provide minimal context
vi.mock("@/store/AppContext", () => ({
  AppProvider: ({ children }: { children: React.ReactNode }) => children,
  useApp: () => ({
    selectedSymbol: "NSE:NIFTY50",
    setSelectedSymbol: vi.fn(),
    env: { mode: "live", environment: "production", dataSource: "API", execution: "DISABLED" },
  }),
  useIndianClock: () => new Date(),
}));

// AuthContext mock
vi.mock("@/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({ user: null, loading: false, signIn: vi.fn(), signOut: vi.fn() }),
}));

describe("Dashboard — all market APIs fail", () => {
  let DashboardPage: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    // All APIs reject — simulate 401/500 network failures
    ds.getQuote.mockRejectedValue(new Error("Authentication required: please sign in"));
    ds.getOHLCV.mockRejectedValue(new Error("Backend server error: 500"));
    ds.getAIAnalysis.mockRejectedValue(new Error("Authentication required: please sign in"));
    ds.getSignals.mockRejectedValue(new Error("Authentication required: please sign in"));
    ds.getFeedHealth.mockRejectedValue(new Error("Backend server error: 500"));
    ds.getPipeline.mockRejectedValue(new Error("Backend server error: 500"));

    const mod = await import("@/pages/Dashboard");
    DashboardPage = mod.DashboardPage;
  });

  it("renders without crashing when quote=500, OHLCV=500, signals=401, analysis=401", async () => {
    const { container } = render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    // Must not throw — ErrorBoundary should NOT be triggered
    expect(container).toBeTruthy();

    // Should show error states instead of crashing
    await waitFor(() => {
      expect(screen.getByText("Unable to load chart")).toBeTruthy();
    });
  });

  it("renders DataHealthPanel error state when feed health fails", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      const panels = screen.getAllByText("Data health unavailable");
      expect(panels.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("renders TickerStrip without crashing", async () => {
    const { container } = render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByLabelText("Market ticker")).toBeTruthy();
    });
  });

  it("renders MarketStatusPanel without crashing", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("Market Status")).toBeTruthy();
    });
  });

  it("does not trigger ErrorBoundary (no 'Something went wrong')", async () => {
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.queryByText("Something went wrong")).toBeNull();
    });
  });
});

describe("Dashboard — partial API responses (200 with missing fields)", () => {
  let DashboardPage: any;

  beforeEach(async () => {
    vi.clearAllMocks();
    // Quote returns 200 with missing price — MarketDataSource should throw
    ds.getQuote.mockResolvedValue({
      symbol: "NSE:NIFTY50",
      providerSymbol: "",
      name: "NIFTY 50",
      exchange: "NSE",
      instrumentType: "index",
      price: undefined,
      previousClose: undefined,
      change: undefined,
      changePct: undefined,
      dayOpen: undefined,
      dayHigh: undefined,
      dayLow: undefined,
      volume: undefined,
      vwap: undefined,
      dayRange: "—",
      volatility: 0,
      sessionState: "REGULAR",
      lastUpdate: 0,
    } as any);
    ds.getOHLCV.mockRejectedValue(new Error("Backend server error: 500"));
    ds.getAIAnalysis.mockRejectedValue(new Error("Authentication required"));
    ds.getSignals.mockRejectedValue(new Error("Authentication required"));
    ds.getFeedHealth.mockRejectedValue(new Error("Backend server error: 500"));
    ds.getPipeline.mockResolvedValue([]);

    const mod = await import("@/pages/Dashboard");
    DashboardPage = mod.DashboardPage;
  });

  it("does not crash when quote has undefined numeric fields", async () => {
    const { container } = render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(container).toBeTruthy();
    });
    // No "Something went wrong" from ErrorBoundary
    expect(screen.queryByText("Something went wrong")).toBeNull();
  });
});
