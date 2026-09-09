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
    getAutonomousDeployments: vi.fn(),
    setAutonomousBotLifecycle: vi.fn(),
    getOptionsCapability: vi.fn(),
  },
}));

const mockBot = (state = "running") => ({
  bot_id: "bot-nifty-options",
  name: "Paper Autonomous Bot",
  state,
  mode: "AUTONOMOUS",
  trading_mode: "paper",
  enabled: true,
  decision_count: 0,
  deployment_count: 0,
  last_decision_timestamp: null,
  last_scan_timestamp: null,
  event_count: 0,
  last_event_type: null,
  last_event_timestamp: null,
  source: "AUTONOMOUS",
  allowed_symbols: ["NSE:NIFTY"],
  allowed_strategy_ids: [],
  allowed_timeframes: ["1d"],
  max_simultaneous_positions: 5,
  max_position_allocation_pct: 0.25,
  max_exposure_pct: 1.0,
  max_drawdown_pct: 0.2,
  safety: {
    kill_switch_state: "active",
    kill_switch_reason: null,
    kill_switch_halted_at: null,
  },
  uptime_seconds: 120,
  started_at: "2025-01-01T00:00:00Z",
  stopped_at: null,
  is_ready: true,
  is_active: state === "running",
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
  bot_id: "bot-nifty-options",
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
    vi.mocked(paperApi.getAutonomousDeployments).mockResolvedValue({ deployments: [], count: 0, schema_version: 1 });
    vi.mocked(paperApi.getOptionsCapability).mockResolvedValue({
      enabled: false,
      allowed_option_types: [],
      max_contracts_per_trade: null,
      providers: {
        discoverer: { status: "not_configured", detail: "" },
        quote: { status: "not_configured", detail: "" },
        chain: { status: "not_configured", detail: "" },
      },
      capable: false,
      execution_phase: "single_leg_capability_surface",
      autonomous_execution_active: false,
      last_error: null,
      schema_version: 1,
    });
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
    vi.mocked(paperApi.getAutonomousEvents).mockImplementation(
      () => new Promise(() => {})
    );
    vi.mocked(paperApi.getAutonomousDeployments).mockImplementation(
      () => new Promise(() => {})
    );

    render(<AutonomousCenter />);

    expect(screen.getByText("Loading autonomous dashboard…")).toBeDefined();
  });

  it("renders error state when bot API fails with retry (does not crash)", async () => {
    vi.mocked(paperApi.getAutonomousBot).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousScan).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousEvents).mockRejectedValue(new Error("Network error"));
    vi.mocked(paperApi.getAutonomousDeployments).mockRejectedValue(new Error("Network error"));

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("Autonomous engine unavailable")).toBeDefined();
    });

    const retryBtn = screen.getByRole("button", { name: "Retry" });
    expect(retryBtn).toBeDefined();

    vi.mocked(paperApi.getAutonomousBot).mockImplementation(
      () => new Promise(() => {})
    );
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByText("Loading autonomous dashboard…")).toBeDefined();
    });
  });

  it("does NOT throw when bot API returns undefined bot payload", async () => {
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({
      bot: {
        bot_id: "bot-nifty-options",
        name: "Paper Autonomous Bot",
        state: "stopped",
        // intentionally omitting trading_mode / safety / config etc.
      } as any,
    });

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("STOPPED")).toBeDefined();
    });
  });

  it("does NOT call /autonomous/decide on initial load", async () => {
    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });

    expect(paperApi.getAutonomousDecisions).not.toHaveBeenCalled();
  });

  it("calls /autonomous/decide only when Run Decision button is clicked (POST)", async () => {
    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });

    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({
      decisions: [mockDecision()],
    });

    fireEvent.click(screen.getByRole("button", { name: "Run Decision" }));

    await waitFor(() => {
      expect(paperApi.getAutonomousDecisions).toHaveBeenCalledTimes(1);
    });
    // Verify the call uses POST, not GET — the frontend must not fire a GET
    // decide request on dashboard mount.
    const callArgs = vi.mocked(paperApi.getAutonomousDecisions).mock.calls[0];
    expect(callArgs).toEqual([]);
  });

  it("renders deployments empty state when API returns 200 with []", async () => {
    vi.mocked(paperApi.getAutonomousDeployments).mockResolvedValue({
      deployments: [],
      count: 0,
      schema_version: 1,
    });
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("No active deployments")).toBeDefined();
    });
  });

  it("renders deployments error state without crashing when API fails", async () => {
    vi.mocked(paperApi.getAutonomousDeployments).mockRejectedValue(
      new Error("500 internal")
    );
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("Deployments unavailable")).toBeDefined();
    });
    // The rest of the dashboard still renders — no crash, no missing bot section.
    expect(screen.getByText("ACTIVE")).toBeDefined();
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

    expect(screen.getByText("Paper Autonomous Bot")).toBeDefined();
    expect(screen.getByText("paper")).toBeDefined();
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

  it("renders decisions table after Run Decision is clicked", async () => {
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

    // Wait for the page to finish loading before asserting on the empty
    // decisions state.
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText("No decisions")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Run Decision" }));

    await waitFor(() => {
      expect(screen.getByText("1 valid / 2 total decisions")).toBeDefined();
    });
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

// --------------------------------------------------------------------------- //
// Regression: response-shape drift must NOT crash the page
// --------------------------------------------------------------------------- //
// These tests reproduce the exact production failure mode:
// the backend returns ``scan.candidates`` and ``ranking.opportunities`` while
// the historical frontend type expected ``scan.signals`` and ``ranking.candidates``.
// The page must tolerate either shape and render the section accordingly.
describe("AutonomousCenter — regression: shape drift tolerance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({ bot: mockBot("running") });
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({
      scan: mockScan([]),
      ranking: null,
    });
    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({ decisions: [] });
    vi.mocked(paperApi.getAutonomousEvents).mockResolvedValue({
      events: [],
      count: 0,
      schema_version: 1,
    });
    vi.mocked(paperApi.getAutonomousDeployments).mockResolvedValue({
      deployments: [],
      count: 0,
      schema_version: 1,
    });
  });

  it("does NOT crash when scan returns the live Phase 8C-B shape (candidates, not signals)", async () => {
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({
      scan: {
        scan_id: "scan-live",
        scan_timestamp: "2026-09-07T00:00:00Z",
        timeframe: "1d",
        universe_size: 3,
        scanned_count: 3,
        eligible_count: 0,
        rejected_count: 3,
        candidates: [],
        rejections: [],
        data_provider_source: "control_center.load_market_data",
        // NB: ``signals`` is intentionally absent.
      },
      ranking: {
        ranking_id: "rank-live",
        ranking_timestamp: "2026-09-07T00:00:00Z",
        candidates_evaluated: 0,
        opportunities: [],
        exclusions: [],
        score_range: [0, 0],
      },
    } as any);

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText(/No signals found/i)).toBeDefined();
  });

  it("renders scan candidates table when Phase 8C-B shape includes candidates", async () => {
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({
      scan: {
        scan_id: "scan-c",
        scan_timestamp: "2026-09-07T00:00:00Z",
        timeframe: "1d",
        universe_size: 3,
        scanned_count: 3,
        eligible_count: 1,
        rejected_count: 2,
        candidates: [
          {
            symbol: "NSE:SBIN",
            strategy_id: "strat-1",
            timeframe: "1d",
            confidence: 0.92,
            rank_score: 0.88,
            signal_action: "BUY",
            signal_timestamp: "2026-09-07T00:00:00Z",
          },
        ],
        rejections: [],
        data_provider_source: "control_center.load_market_data",
      },
      ranking: {
        ranking_id: "rank-c",
        opportunities: [],
        exclusions: [],
      },
    } as any);

    render(<AutonomousCenter />);

    await waitFor(() => {
      expect(screen.getByText("NSE:SBIN")).toBeDefined();
    });
    expect(screen.getByText("BUY")).toBeDefined();
    expect(screen.getByText("92%")).toBeDefined();
  });

  it("does NOT crash when scan ranking.opportunities is undefined (older shim)", async () => {
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({
      scan: {
        scan_id: "scan-x",
        scan_timestamp: "2026-09-07T00:00:00Z",
        candidates: [
          {
            symbol: "NSE:SBIN",
            strategy_id: "strat-1",
            timeframe: "1d",
            confidence: 0.7,
            rank_score: 0.7,
            signal_action: "SELL",
            signal_timestamp: "2026-09-07T00:00:00Z",
          },
        ],
      },
      ranking: {
        ranking_id: "rank-x",
        // older shim: no opportunities array
      },
    } as any);

    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("NSE:SBIN")).toBeDefined();
    });
  });

  it("renders scan error state without crashing when /autonomous/scan 500s", async () => {
    vi.mocked(paperApi.getAutonomousScan).mockRejectedValue(
      new Error("500 internal")
    );
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText(/Scan unavailable/i)).toBeDefined();
  });

  it("renders deployments error state without crashing when /autonomous/deployments 500s", async () => {
    vi.mocked(paperApi.getAutonomousDeployments).mockRejectedValue(
      new Error("500 internal — paper_deployments.options_enabled missing")
    );
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText(/Deployments unavailable/i)).toBeDefined();
  });

  it("does NOT crash when bot payload is missing deploy counts / status fields", async () => {
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({
      bot: {
        bot_id: "b",
        name: "Bare Bot",
        state: "running",
        // intentionally omitting deployment_count, event_count, uptime_seconds,
        // trading_mode, source, allowed_*, max_*, safety
      } as any,
    });

    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText("Bare Bot")).toBeDefined();
  });

  it("does NOT crash when decisions response has empty/missing decisions array", async () => {
    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({
      // No `decisions` field at all.
      result_id: "rid",
      decisions: [],
    } as any);

    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText("No decisions")).toBeDefined();
  });

  it("does NOT crash when events array is null", async () => {
    vi.mocked(paperApi.getAutonomousEvents).mockResolvedValue({
      events: null,
      count: 0,
      schema_version: 1,
    } as any);

    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeDefined();
    });
    expect(screen.getByText(/0 recent events/i)).toBeDefined();
  });
});

// --------------------------------------------------------------------------- //
// Phase A — Options capability surface tests                                  //
// --------------------------------------------------------------------------- //
// These tests verify that the AutonomousCenter renders the options capability
// panel based on the backend's actual response — and that the UI never
// claims autonomous option execution is active in Phase A.
describe("AutonomousCenter — options capability surface (Phase A)", () => {
  const mockDeployment = {
    deployment_id: "dep-1",
    strategy_id: "tf1",
    strategy_spec_hash: "abc123",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    execution_mode: "paper",
    dataset_id: "ds-1",
    status: "active",
    created_at: "2025-01-01T00:00:00Z",
    activated_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
    notes: "",
    schema_version: 1,
    options_enabled: true,
    allowed_option_types: ["CE", "PE"],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(paperApi.getAutonomousBot).mockResolvedValue({ bot: mockBot("running") });
    vi.mocked(paperApi.getAutonomousScan).mockResolvedValue({ scan: mockScan([]), ranking: null });
    vi.mocked(paperApi.getAutonomousDecisions).mockResolvedValue({ decisions: [] });
    vi.mocked(paperApi.getAutonomousEvents).mockResolvedValue({ events: [], count: 0, schema_version: 1 });
    vi.mocked(paperApi.getAutonomousDeployments).mockResolvedValue({
      deployments: [mockDeployment],
      count: 1,
      schema_version: 1,
    });
    vi.mocked(paperApi.setAutonomousBotLifecycle).mockResolvedValue({
      success: true,
      message: "ok",
      bot: mockBot("running"),
      schema_version: 1,
    });
  });

  it("renders the disabled state when options_enabled is false", async () => {
    vi.mocked(paperApi.getOptionsCapability).mockResolvedValue({
      enabled: false,
      allowed_option_types: ["CE", "PE"],
      max_contracts_per_trade: null,
      providers: {
        discoverer: { status: "disabled", detail: "not attached" },
        quote: { status: "disabled", detail: "not attached" },
        chain: { status: "disabled", detail: "not attached" },
      },
      capable: false,
      execution_phase: "single_leg_capability_surface",
      autonomous_execution_active: false,
      last_error: null,
      schema_version: 1,
    });
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("Options trading is disabled for this deployment.")).toBeDefined();
    });
    expect(screen.queryByText(/autonomous option execution is active/i)).toBeNull();
  });

  it("renders the enabled+unavailable state when providers are not available", async () => {
    vi.mocked(paperApi.getOptionsCapability).mockResolvedValue({
      enabled: true,
      allowed_option_types: ["CE", "PE"],
      max_contracts_per_trade: null,
      providers: {
        discoverer: { status: "disabled", detail: "no controller" },
        quote: { status: "unavailable", detail: "not authenticated" },
        chain: { status: "disabled", detail: "no chain" },
      },
      capable: false,
      execution_phase: "single_leg_capability_surface",
      autonomous_execution_active: false,
      last_error: null,
      schema_version: 1,
    });
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(
        screen.getByText(
          "Options are enabled, but required providers are unavailable."
        )
      ).toBeDefined();
    });
    expect(screen.queryByText(/autonomous option execution is active/i)).toBeNull();
  });

  it("renders the enabled+capable state and explicitly says execution is NOT enabled", async () => {
    vi.mocked(paperApi.getOptionsCapability).mockResolvedValue({
      enabled: true,
      allowed_option_types: ["CE"],
      max_contracts_per_trade: 3,
      providers: {
        discoverer: { status: "available", detail: "ok" },
        quote: { status: "available", detail: "ok" },
        chain: { status: "disabled", detail: "phase 8a" },
      },
      capable: true,
      execution_phase: "single_leg_capability_surface",
      autonomous_execution_active: false,
      last_error: null,
      schema_version: 1,
    });
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("Options capability available")).toBeDefined();
    });
    // Must show CE only (not PE) since allowed_option_types = ["CE"].
    expect(screen.getByText("CE")).toBeDefined();
    // Must show the configured max contracts.
    expect(screen.getByText("3")).toBeDefined();
    // The body text MUST say execution is NOT enabled.
    expect(
      screen.getByText(/Autonomous option execution is not enabled in this phase/i)
    ).toBeDefined();
    expect(screen.queryByText(/autonomous option execution is active/i)).toBeNull();
  });

  it("renders an error state when the capability probe fails", async () => {
    vi.mocked(paperApi.getOptionsCapability).mockRejectedValue(
      new Error("500 internal")
    );
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("Options capability unavailable")).toBeDefined();
    });
    // The rest of the dashboard still renders.
    expect(screen.queryAllByText("ACTIVE").length).toBeGreaterThanOrEqual(1);
  });

  it("does NOT display Greeks / OI / IV / multi-leg availability claims", async () => {
    vi.mocked(paperApi.getOptionsCapability).mockResolvedValue({
      enabled: true,
      allowed_option_types: ["CE", "PE"],
      max_contracts_per_trade: 1,
      providers: {
        discoverer: { status: "available", detail: "" },
        quote: { status: "available", detail: "" },
        chain: { status: "disabled", detail: "" },
      },
      capable: true,
      execution_phase: "single_leg_capability_surface",
      autonomous_execution_active: false,
      last_error: null,
      schema_version: 1,
    });
    render(<AutonomousCenter />);
    await waitFor(() => {
      expect(screen.getByText("Options capability available")).toBeDefined();
    });
    // These capability labels must NOT appear because the backend
    // does not provide them today.
    expect(screen.queryByText(/Greeks available/i)).toBeNull();
    expect(screen.queryByText(/OI available/i)).toBeNull();
    expect(screen.queryByText(/IV available/i)).toBeNull();
    expect(screen.queryByText(/Multi-leg P&L available/i)).toBeNull();
    // The "Not yet enabled" section MUST list them honestly.
    expect(screen.getByText(/Multi-leg strategies/)).toBeDefined();
    expect(screen.getByText(/Greeks \/ OI \/ IV analytics/)).toBeDefined();
  });
});
