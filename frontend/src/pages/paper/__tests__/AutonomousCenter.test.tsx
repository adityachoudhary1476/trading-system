import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import AutonomousCenter from "@/pages/paper/AutonomousCenter";
import { paperApi } from "@/lib/paperApi";

vi.mock("@/lib/paperApi", () => ({
  paperApi: {
    getAutonomousBot: vi.fn(),
    getAutonomousScan: vi.fn(),
    getAutonomousDecisions: vi.fn(),
    getAutonomousEvents: vi.fn(),
    setAutonomousBotLifecycle: vi.fn(),
  },
}));

const mockBot = (status = "running") => ({
  bot_id: "bot-paper-default",
  name: "Paper Autonomous Bot",
  status,
  config: {
    bot_id: "bot-paper-default",
    name: "Paper Autonomous Bot",
    mode: "AUTONOMOUS",
    trading_mode: "PAPER",
    enabled: true,
    source: "AUTONOMOUS",
    constraints: {
      allowed_symbols: ["NSE:SBIN", "NSE:TCS"],
      allowed_timeframes: ["1d", "1h"],
      allowed_strategy_ids: [],
      max_simultaneous_positions: 5,
    },
  },
  current_scan: null,
  current_ranking: null,
  current_decisions: null,
  deployments: [],
  deployment_count: 0,
  policies: {},
  event_count: 0,
  last_event_type: null,
  last_event_timestamp: null,
  uptime_seconds: 120,
  started_at: "2025-01-01T00:00:00Z",
  stopped_at: null,
  is_ready: true,
  is_active: true,
});

const mockScan = (signals: any[] = []) => ({
  scan_id: "scan_123",
  timestamp: "2025-01-01T00:00:00Z",
  signals,
  scan_metadata: {},
  total_symbols: 50,
  total_signals: signals.length,
  elapsed_ms: 150,
});

const mockSignal = (overrides: Record<string, unknown> = {}) => ({
  symbol: "NSE:SBIN",
  timeframe: "1d",
  action: "BUY",
  confidence: 0.85,
  strategy_id: "tf1",
  signal_timestamp: "2025-01-01T00:00:00Z",
  ...overrides,
});

const mockDecision = (overrides: Record<string, unknown> = {}) => ({
  decision_id: "dec_123",
  symbol: "NSE:SBIN",
  strategy_id: "tf1",
  timeframe: "1d",
  action: "BUY",
  confidence: 0.85,
  is_valid: true,
  status: "approved",
  decision_timestamp: "2025-01-01T00:00:00Z",
  signal_timestamp: "2025-01-01T00:00:00Z",
  signal: mockSignal(),
  ...overrides,
});

const mockEvent = (overrides: Record<string, unknown> = {}) => ({
  event_id: "evt_123",
  bot_id: "bot-paper-default",
  event_type: "BOT_STARTED",
  timestamp: "2025-01-01T00:00:00Z",
  deployment_id: null,
  symbol: null,
  timeframe: null,
  message: "Bot started",
  payload: {},
  ...overrides,
});

describe("AutonomousCenter — Autonomous Trading Operations Center", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({ bot: mockBot("running") });
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({ scan: mockScan([]), ranking: null });
    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({ decisions: [] });
    vi.mocked(paperApi.getAutonomousEvents).mockResolvedValue({ events: [], count: 0, schema_version: 1 });
    vi.mocked(paperApi.setAutonomousBotLifecycle).mockResolvedValue({
      success: true,
      message: "ok",
      bot: mockBot("running"),
      schema_version: 1,
    });
  });

  it("renders loading state initially", () => {
    vi.mocked(paperApi.getAutonomousBot).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getAutonomousScan).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getAutonomousDecisions).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getAutonomousEvents).mockImplementation(
      () => new Promise(() => {})
    );

    render(<AutonomousCenter />);

    expect(screen.getByText("Loading autonomous dashboard…")).toBeDefined();
  });

  it("renders error state when API fails with retry", async () => {
    vi.mocked(paperApi.getAutonomousBot).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousScan).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousDecisions).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousEvents).mockRejectedValue(new Error("Network error"));

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load autonomous dashboard")).toBeDefined();
    });

    const retryBtn = screen.getByRole("button", { name: "Retry" });
    expect(retryBtn).toBeDefined();

    // Clicking retry re-triggers fetchAll which shows loading state
    vi.mocked(paperApi.getAutonomousBot).mockImplementation(
      () => new Promise(() => {})
    );
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByText("Loading autonomous dashboard…")).toBeDefined();
    });
  });

  it("renders empty states when no data", async () => {
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({ bot: mockBot("stopped") });

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("STOPPED")).toBeDefined();
    });

    expect(screen.getByText("No signals found")).toBeDefined();
    expect(screen.getByText("No decisions")).toBeDefined();
    expect(screen.getByText("No events")).toBeDefined();
  });

  it("renders bot status and lifecycle controls when running", async () => {
    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });

    expect(screen.getByRole("button", { name: "Pause" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDefined();

    // Bot config metrics
    expect(screen.getByText("Paper Autonomous Bot")).toBeDefined();
    expect(screen.getByText("PAPER")).toBeDefined();
  });

  it("renders scan signals table with multiple signals", async () => {
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({
      scan: mockScan([
        mockSignal({ symbol: "NSE:SBIN", action: "BUY" }),
        mockSignal({ symbol: "NSE:TCS", action: "SELL" }),
      ]),
      ranking: null,
    });

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("Current Market Scan")).toBeDefined();
    });

    expect(screen.getByText("NSE:SBIN")).toBeDefined();
    expect(screen.getByText("NSE:TCS")).toBeDefined();
    expect(screen.getByText("BUY")).toBeDefined();
    expect(screen.getByText("SELL")).toBeDefined();
  });

  it("renders decisions table with valid and invalid entries", async () => {
    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({
      decisions: [
        mockDecision({ decision_id: "dec_valid", symbol: "NSE:SBIN", is_valid: true }),
        mockDecision({
          decision_id: "dec_invalid",
          symbol: "NSE:TCS",
          is_valid: false,
          exclusion_reason: "policy_violation",
        }),
      ],
    });

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("Trading Decisions")).toBeDefined();
    });

    expect(screen.getByText("1 valid / 2 total decisions")).toBeDefined();
    expect(screen.getByText("NSE:SBIN")).toBeDefined();
    expect(screen.getByText("NSE:TCS")).toBeDefined();
    expect(screen.getByText("policy_violation")).toBeDefined();
  });

  it("renders event log with multiple entries", async () => {
    vi.mocked(paperApi.getAutonomousEvents).mockResolvedValue({
      events: [
        mockEvent({ event_id: "evt_1", event_type: "BOT_STARTED", message: "Bot started" }),
        mockEvent({ event_id: "evt_2", event_type: "SCAN_COMPLETED", message: "Scan completed" }),
        mockEvent({ event_id: "evt_3", event_type: "DECISION_CREATED", message: "Decision created" }),
      ],
      count: 3,
      schema_version: 1,
    });

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("BOT_STARTED")).toBeDefined();
    });

    expect(screen.getByText("SCAN_COMPLETED")).toBeDefined();
    expect(screen.getByText("DECISION_CREATED")).toBeDefined();
    expect(screen.getByText("3 recent events")).toBeDefined();
  });
});
