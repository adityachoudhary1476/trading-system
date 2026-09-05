import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PaperStrategies } from "@/pages/paper/PaperStrategies";
import { paperApi } from "@/lib/paperApi";

vi.mock("@/lib/paperApi", () => ({
  paperApi: {
    getStrategies: vi.fn(),
    getRegime: vi.fn(),
    getAllocation: vi.fn(),
    listDeployments: vi.fn(),
    getDashboard: vi.fn(),
    getEvidence: vi.fn(),
  },
}));

const mockStrategies = [
  {
    name: "trend_following_ema",
    strategy_id: "tf1",
    spec_name: "Phase22_TrendFollowing_EMA",
    description: "Trend following via EMA",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    indicators: ["ema_9", "ema_21"],
    entry_condition: "crosses_above",
    allow_short: true,
    generated_by: "phase22",
  },
  {
    name: "momentum_rsi",
    strategy_id: "mom1",
    spec_name: "Phase22_Momentum_RSI",
    description: "Momentum via RSI",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    indicators: ["rsi_14"],
    entry_condition: ">",
    allow_short: true,
    generated_by: "phase22",
  },
];

const mockRegime = {
  regime: "trending_up",
  confidence: 0.85,
  features: ["trend=up"],
  warnings: [],
  regime_at_ms: Date.now(),
};

const mockAllocation = {
  regime: "trending_up",
  regime_confidence: 0.85,
  regime_fit: 0.85,
  total_strategies_available: 5,
  timestamp_ms: Date.now(),
  selected_strategies: [
    {
      strategy_name: "trend_following_ema",
      category: "trend_following",
      regime_compatibility: 1.0,
      research_score: 0.8,
      weight: 0.6,
    },
    {
      strategy_name: "breakout_nbar",
      category: "breakout",
      regime_compatibility: 0.9,
      research_score: 0.7,
      weight: 0.4,
    },
  ],
};

describe("PaperStrategies — Strategy Leaderboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading states while fetching", () => {
    vi.mocked(paperApi.getStrategies).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getRegime).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getAllocation).mockImplementation(
      () => new Promise(() => {})
    );
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    expect(screen.getByText("Loading strategies...")).toBeDefined();
    expect(screen.getByText("Loading market regime...")).toBeDefined();
    expect(screen.getByText("Loading strategy allocation...")).toBeDefined();
  });

  it("renders strategy leaderboard after loading", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: true,
      data: mockStrategies,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({
      ok: true,
      data: mockRegime,
    } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({
      ok: true,
      data: mockAllocation,
    } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText("Strategy Intelligence")).toBeDefined();
    });
    expect(screen.getByText("Phase22_TrendFollowing_EMA")).toBeDefined();
    expect(screen.getByText("Phase22_Momentum_RSI")).toBeDefined();
  });

  it("shows regime when market data is available", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: true,
      data: mockStrategies,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({
      ok: true,
      data: mockRegime,
    } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({
      ok: true,
      data: mockAllocation,
    } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText("trending_up")).toBeDefined();
    });
    expect(screen.getAllByText("85%").length).toBeGreaterThanOrEqual(1);
  });

  it("shows regime unavailable message when no market data provider", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: true,
      data: mockStrategies,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({
      ok: false,
      error: { code: "bad_request", message: "no market data provider configured" },
      status: 400,
    } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({
      ok: false,
      error: { code: "bad_request", message: "no market data provider configured" },
      status: 400,
    } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText(/Market regime unavailable/)).toBeDefined();
    });
    expect(screen.getByText(/Strategy allocation unavailable/)).toBeDefined();
  });

  it("shows error state when strategies fetch fails", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: false,
      error: { code: "network_error", message: "Network request failed" },
      status: 0,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({ ok: false, error: { code: "network_error", message: "" }, status: 0 } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({ ok: false, error: { code: "network_error", message: "" }, status: 0 } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText("Unable to load strategies")).toBeDefined();
    });
  });

  it("shows recommended allocation strategies when available", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: true,
      data: mockStrategies,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({
      ok: true,
      data: mockRegime,
    } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({
      ok: true,
      data: mockAllocation,
    } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText("Recommended Allocation")).toBeDefined();
    });
    expect(screen.getByText("Strategy Allocation")).toBeDefined();
    expect(screen.getByText("5")).toBeDefined(); // total_strategies_available
  });

  it("renders DEMO/SIMULATED label", async () => {
    vi.mocked(paperApi.getStrategies).mockResolvedValue({
      ok: true,
      data: mockStrategies,
    } as any);
    vi.mocked(paperApi.getRegime).mockResolvedValue({
      ok: false,
      error: { code: "bad_request", message: "no market data" },
      status: 400,
    } as any);
    vi.mocked(paperApi.getAllocation).mockResolvedValue({
      ok: false,
      error: { code: "bad_request", message: "no market data" },
      status: 400,
    } as any);
    render(
      <MemoryRouter initialEntries={["/paper/strategies"]}>
        <PaperStrategies />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(screen.getByText(/DEMO \/ SIMULATED/)).toBeDefined();
    });
  });
});
