import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";
import type { DeploymentStatus, SessionStatus, HealthStatus, CircuitState, RiskDecision } from "@/types/paper-api";
import { PaperTradingPage } from "@/pages/paper/PaperTrading";
import { PaperOverview } from "@/pages/paper/PaperOverview";
import { PaperDeployments } from "@/pages/paper/PaperDeployments";
import { PaperDeploymentDetail } from "@/pages/paper/PaperDeploymentDetail";
import { PaperSessions } from "@/pages/paper/PaperSessions";
import { PaperPositions } from "@/pages/paper/PaperPositions";
import { PaperEvents } from "@/pages/paper/PaperEvents";
import { PaperRiskHealth } from "@/pages/paper/PaperRiskHealth";
import { PaperReports } from "@/pages/paper/PaperReports";
import { PaperStrategies } from "@/pages/paper/PaperStrategies";
import { Sidebar } from "@/components/layout/Sidebar";
import { AppProvider } from "@/store/AppContext";
import { paperApi } from "@/lib/paperApi";

vi.mock("@/lib/paperApi", () => ({
  paperApi: {
    getDashboard: vi.fn(),
    listDeployments: vi.fn(),
    getDeployment: vi.fn(),
    getSession: vi.fn(),
    getPositions: vi.fn(),
    getEvents: vi.fn(),
    getHealth: vi.fn(),
    getRisk: vi.fn(),
    getCircuitBreaker: vi.fn(),
    getEvidence: vi.fn(),
    exportJson: vi.fn(),
    activate: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    stop: vi.fn(),
    checkpoint: vi.fn(),
    restore: vi.fn(),
    resetCircuitBreaker: vi.fn(),
  },
}));

const renderWithRouter = (ui: React.ReactElement) =>
  render(<BrowserRouter>{ui}</BrowserRouter>);

const renderWithParams = (ui: React.ReactElement, path: string, paramName = ":deploymentId") => {
  const paramPath = path.replace(/\/[^/]+$/, `/${paramName}`);
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={paramPath} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
};

const mockSnapshot = {
  generated_at: "2024-01-01T00:00:00Z",
  deployment: {
    deployment_id: "dep-1",
    strategy_id: "strat-1",
    strategy_spec_hash: "hash-1",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    execution_mode: "paper",
    dataset_id: "ds-1",
    status: "active" as DeploymentStatus,
    created_at: "2024-01-01T00:00:00Z",
    activated_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    notes: "",
    schema_version: 1,
  },
  strategy: {
    strategy_id: "strat-1",
    spec_hash: "hash-1",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    lifecycle_status: "active",
    generated_by: "test",
    name: "Test Strategy",
    description: "",
    parent_strategy_id: "",
    latest_evidence_at: null,
    latest_research_evidence_id: null,
    latest_walk_forward_evidence_id: null,
    latest_paper_evidence_id: null,
  },
  session: {
    session_id: "sid-1",
    deployment_id: "dep-1",
    strategy_id: "strat-1",
    strategy_spec_hash: "hash-1",
    symbol: "NSE:SBIN",
    timeframe: "1d",
    execution_mode: "paper",
    dataset_id: "ds-1",
    session_status: "active" as SessionStatus,
    deployment_status: "active" as DeploymentStatus,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    last_processed_bar_timestamp: "2024-01-01T00:00:00Z",
    bar_count: 10,
    generated_signals: 5,
    orders_submitted: 3,
    fills_received: 2,
    rejected_orders: 0,
    starting_equity: 100000,
    current_equity: 100500,
    realized_pnl: 500,
    unrealized_pnl: 0,
    max_drawdown: -100,
    health_status: "healthy" as HealthStatus,
    halt_reason: null,
    consecutive_errors: 0,
    circuit_state: "closed" as CircuitState,
    circuit_reason: null,
    circuit_trip_count: 0,
    event_count: 5,
    event_sequence: 4,
    broker_state: {},
    operations_state_json: {},
    schema_version: 3,
  },
  account: {
    initial_cash: 100000,
    cash: 100000,
    equity: 100500,
    margin_used: 0,
    available_cash: 100000,
    realized_pnl: 500,
    unrealized_pnl: 0,
    starting_equity: 100000,
    total_return: 0.005,
  },
  positions: {
    open_position: null,
    is_flat: true,
  },
  performance: {
    realized_pnl: 500,
    unrealized_pnl: 0,
    total_pnl: 500,
    return: 0.005,
    drawdown: -0.001,
    trade_count: 2,
    win_rate: 1,
    profit_factor: 2,
    exposure: 0,
    health_status: "healthy" as HealthStatus,
    orders_submitted: 3,
    fills_received: 2,
    rejected_orders: 0,
    generated_signals: 5,
    bar_count: 10,
  },
  health: {
    status: "healthy" as HealthStatus,
    halt_reason: null,
    warnings: [],
  },
  risk: {
    decision: "allow" as RiskDecision,
    reason: null,
  },
  circuit_breaker: {
    state: "closed" as CircuitState,
    reason: null,
    trip_count: 0,
  },
  recent_events: {
    total_events: 5,
    last_event_sequence: 4,
    last_event_type: "bar_processed",
    last_event_timestamp: "2024-01-01T00:00:00Z",
    recent: [],
  },
  evidence_summary: {
    research_count: 1,
    walk_forward_count: 1,
    paper_trading_count: 0,
    latest_research_evidence_id: null,
    latest_walk_forward_evidence_id: null,
    latest_paper_trading_evidence_id: null,
    latest_research_at: null,
    latest_walk_forward_at: null,
    latest_paper_trading_at: null,
  },
  schema_version: 1,
  session_schema_version: 3,
} as const;

const mockDeployments = {
  deployments: [
    {
      deployment_id: "dep-1",
      strategy_id: "strat-1",
      strategy_spec_hash: "hash-1",
      symbol: "NSE:SBIN",
      timeframe: "1d",
      execution_mode: "paper",
      dataset_id: "ds-1",
      status: "active" as DeploymentStatus,
      created_at: "2024-01-01T00:00:00Z",
      activated_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
      notes: "",
      schema_version: 1,
    },
  ],
  count: 1,
  schema_version: 1,
};

describe("Paper Trading Frontend", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
  });

  describe("PaperTradingPage layout", () => {
    it("renders paper-only terminal banner", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.getByText("Paper Trading")).toBeDefined();
      expect(screen.getByText(/Autonomous simulated execution/)).toBeDefined();
      expect(screen.getByText(/PAPER EXECUTION CONSOLE/)).toBeDefined();
    });

    it("renders grouped navigation in sidebar", () => {
      // Sidebar navigation is rendered by App, not PaperTradingPage
      render(
        <MemoryRouter initialEntries={["/paper/overview"]}>
          <AppProvider>
            <Sidebar />
          </AppProvider>
        </MemoryRouter>,
      );
      // Group headers
      expect(screen.getByText("Operations")).toBeDefined();
      expect(screen.getByText("Monitoring")).toBeDefined();
      expect(screen.getByText("Reporting")).toBeDefined();
      // Nav items
      expect(screen.getByText("Overview")).toBeDefined();
      expect(screen.getByText("Deployments")).toBeDefined();
      expect(screen.getByText("Risk & Health")).toBeDefined();
      expect(screen.getByText("Reports")).toBeDefined();
    });

    it("shows paper environment badge", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.getByText(/No Real Money/)).toBeDefined();
    });

    it("shows terminal header with session pulse", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.getByText("AETHER // AUTONOMOUS TERMINAL")).toBeDefined();
      expect(screen.getByText(/Live Paper|Paper Halted/)).toBeDefined();
    });
  });

  describe("PaperDeployments", () => {
    it("renders deployment list", async () => {
      renderWithRouter(<PaperDeployments />);

      await waitFor(() => {
        expect(screen.getByText(/Paper Deployments/)).toBeDefined();
      });
      expect(screen.getByText("strat-1")).toBeDefined();
    });

    it("shows empty state when no deployments", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: { deployments: [], count: 0, schema_version: 1 } } as any);

      renderWithRouter(<PaperDeployments />);

      await waitFor(() => {
        expect(screen.getByText("No deployments")).toBeDefined();
      });
    });

    it("shows error on API failure", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: false, error: { code: "network_error", message: "Failed" }, status: 0 } as any);

      renderWithRouter(<PaperDeployments />);

      await waitFor(() => {
        expect(screen.getByText("Unable to load deployments")).toBeDefined();
      });
    });
  });

  describe("PaperOverview", () => {
    it("renders empty state when no deployment selected", async () => {
      renderWithRouter(<PaperOverview />);
      await waitFor(() => {
        expect(screen.getByText("No paper deployment selected")).toBeDefined();
      });
    });

    it("renders deployment context and KPIs when dashboard loads", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      // Select a deployment to trigger data load
      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getAllByText(/NSE:SBIN/).length).toBeGreaterThan(0);
      });
      // KPI section
      expect(screen.getByText("Account & Performance Telemetry")).toBeDefined();
      expect(screen.getAllByText("Return").length).toBeGreaterThan(0);
      // System status
      expect(screen.getByText("System Status")).toBeDefined();
      // Risk row inside system status
      expect(screen.getAllByText("ALLOW").length).toBeGreaterThan(0);
      // Paper-only identity is preserved (the parent PaperTrading header is
      // not rendered when PaperOverview is mounted standalone in this test).
      expect(screen.getAllByText(/NSE:SBIN/).length).toBeGreaterThan(0);
    });

    it("shows skeleton loading state while fetching dashboard", async () => {
      vi.mocked(paperApi.getDashboard).mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, data: mockSnapshot } as any), 200))
      );

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      // Skeleton should be visible immediately after selection
      expect(screen.getByLabelText("Loading terminal")).toBeDefined();

      // Wait for data to load
      await waitFor(() => {
        expect(screen.getAllByText(/NSE:SBIN/).length).toBeGreaterThan(0);
      });
    });

    it("renders health state correctly", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getAllByText("HEALTHY").length).toBeGreaterThan(0);
      });
    });

    it("renders critical state banner when halted", async () => {
      const haltedSnapshot = {
        ...mockSnapshot,
        health: { status: "halted" as HealthStatus, halt_reason: "Drawdown limit exceeded", warnings: [] },
      };
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: haltedSnapshot } as any);

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getByText("Paper Execution Halted")).toBeDefined();
        expect(screen.getAllByText(/Drawdown limit exceeded/).length).toBeGreaterThan(0);
      });
    });

    it("renders decision timeline with recent events", async () => {
      const withEvents = {
        ...mockSnapshot,
        recent_events: {
          total_events: 2,
          last_event_sequence: 2,
          last_event_type: "fill_received",
          last_event_timestamp: "2024-01-01T00:00:00Z",
          recent: [
            { sequence: 1, timestamp: "2024-01-01T00:00:00Z", event_type: "signal_generated", deployment_id: "dep-1", strategy_id: "strat-1", message: "LONG signal" },
            { sequence: 2, timestamp: "2024-01-01T00:00:01Z", event_type: "fill_received", deployment_id: "dep-1", strategy_id: "strat-1", message: "Filled 10 @ 100" },
          ],
        },
      };
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: withEvents } as any);

      renderWithRouter(<PaperOverview />);

      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getByText("Decision Timeline")).toBeDefined();
        expect(screen.getByText(/signal generated/i)).toBeDefined();
        expect(screen.getByText(/fill received/i)).toBeDefined();
      });
    });

    it("renders no-positions state", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getByText(/No open positions|No Open Positions/)).toBeDefined();
      });
    });

    it("renders equity curve empty state", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getByText("Equity Curve")).toBeDefined();
        expect(screen.getByText(/No Performance History/)).toBeDefined();
      });
    });

    it("shows error state with retry", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue(
        { ok: false, error: { code: "network_error", message: "Connection failed" }, status: 0 } as any
      );

      renderWithRouter(<PaperOverview />);

      // Wait for deployment picker to load options
      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });

      const select = screen.getByLabelText("Select deployment");
      fireEvent.change(select, { target: { value: "dep-1" } });

      await waitFor(() => {
        expect(screen.getByText("Unable to load paper terminal")).toBeDefined();
      });
      expect(screen.getByText("Connection failed")).toBeDefined();
    });
  });

  describe("PaperDeploymentDetail", () => {
    it("renders deployment identity", async () => {
      vi.mocked(paperApi.getDeployment).mockResolvedValue({ ok: true, data: { deployment: mockDeployments.deployments[0], schema_version: 1 } } as any);
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithParams(<PaperDeploymentDetail />, "/paper/deployments/dep-1");

      await waitFor(() => {
        expect(screen.getByText(/dep-1/)).toBeDefined();
      });
      expect(screen.getByText("strat-1")).toBeDefined();
    });

    it("shows lifecycle controls", async () => {
      vi.mocked(paperApi.getDeployment).mockResolvedValue({ ok: true, data: { deployment: mockDeployments.deployments[0], schema_version: 1 } } as any);
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithParams(<PaperDeploymentDetail />, "/paper/deployments/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Activate")).toBeDefined();
      });
      expect(screen.getByText("Stop")).toBeDefined();
      expect(screen.getByText("Checkpoint")).toBeDefined();
      expect(screen.getByText("Restore")).toBeDefined();
    });
  });

  describe("PaperSessions", () => {
    const sessionMock = {
      session: {
        session_id: "sid-1",
        deployment_id: "dep-1",
        strategy_id: "strat-1",
        strategy_spec_hash: "hash-1",
        symbol: "NSE:SBIN",
        timeframe: "1d",
        execution_mode: "paper",
        dataset_id: "ds-1",
        session_status: "active" as SessionStatus,
        deployment_status: "active" as DeploymentStatus,
        created_at: "2024-01-01T00:00:00Z",
        updated_at: "2024-01-01T00:00:00Z",
        last_processed_bar_timestamp: "2024-01-01T00:00:00Z",
        bar_count: 10,
        generated_signals: 5,
        orders_submitted: 3,
        fills_received: 2,
        rejected_orders: 0,
        starting_equity: 100000,
        current_equity: 100500,
        realized_pnl: 500,
        unrealized_pnl: 0,
        max_drawdown: -100,
        health_status: "healthy" as HealthStatus,
        halt_reason: null,
        consecutive_errors: 0,
        circuit_state: "closed" as CircuitState,
        circuit_reason: null,
        circuit_trip_count: 0,
        event_count: 5,
        event_sequence: 4,
        broker_state: {},
        operations_state_json: {},
        schema_version: 3,
      },
      checkpoint: null,
      schema_version: 1,
    };

    const sessionWithCheckpoint = {
      ...sessionMock,
      checkpoint: {
        checkpoint_id: "cp-1",
        session_id: "sid-1",
        deployment_id: "dep-1",
        strategy_id: "strat-1",
        strategy_spec_hash: "hash-1",
        symbol: "NSE:SBIN",
        timeframe: "1d",
        execution_mode: "paper",
        dataset_id: "ds-1",
        schema_version: 3,
        deployment_status: "active" as DeploymentStatus,
        session_status: "active" as SessionStatus,
        last_processed_bar_timestamp: "2024-01-01T00:00:00Z",
        bar_count: 10,
        orders_submitted: 3,
        fills_received: 2,
        rejected_orders: 0,
        generated_signals: 5,
        consecutive_errors: 0,
        starting_equity: 100000,
        current_equity: 100500,
        realized_pnl: 500,
        unrealized_pnl: 0,
        max_drawdown: -100,
        health_status: "healthy" as HealthStatus,
        halt_reason: null,
        circuit_state: "closed" as CircuitState,
        circuit_reason: null,
        circuit_trip_count: 0,
        event_count: 5,
        event_sequence: 4,
        broker_state: {},
        operations_state_json: {},
        events_fingerprint: "fp-1",
        ops_fingerprint: "fp-2",
        created_at: "2024-01-01T00:00:00Z",
      },
    };

    it("renders session identity", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionMock } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getAllByText("sid-1").length).toBeGreaterThan(0);
      });
      expect(screen.getAllByText("strat-1").length).toBeGreaterThan(0);
      expect(screen.getByText(/NSE:SBIN/)).toBeDefined();
      expect(screen.getByText(/1d/)).toBeDefined();
      expect(screen.getByText("PAPER")).toBeDefined();
      expect(screen.getAllByText("ACTIVE").length).toBeGreaterThan(1);
    });

    it("distinguishes session and deployment status", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionMock } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getAllByText("ACTIVE").length).toBeGreaterThan(1);
      });
    });

    it("renders operational metrics", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionMock } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Operational Counters")).toBeDefined();
      });
      expect(screen.getAllByText("10").length).toBeGreaterThan(0);
      expect(screen.getAllByText("5").length).toBeGreaterThan(0);
    });

    it("renders checkpoint section", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionMock } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Current Checkpoint")).toBeDefined();
      });
      expect(screen.getByText("No checkpoint")).toBeDefined();
    });

    it("renders checkpoint data when present", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionWithCheckpoint } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("cp-1")).toBeDefined();
      });
      expect(screen.getAllByText("ds-1").length).toBeGreaterThan(0);
    });

    it("renders restore control", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionWithCheckpoint } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Restore Session")).toBeDefined();
      });
    });

    it("shows restore confirmation dialog", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionWithCheckpoint } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Restore Session")).toBeDefined();
      });
      fireEvent.click(screen.getByText("Restore Session"));
      expect(screen.getByText("Restore session?")).toBeDefined();
    });

    it("shows error state with retry", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: false, error: { code: "network_error", message: "Failed" }, status: 0 } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Unable to load session")).toBeDefined();
      });
      expect(screen.getByText("Failed")).toBeDefined();
    });

    it("shows loading skeleton", async () => {
      vi.mocked(paperApi.getSession).mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, data: sessionMock } as any), 200))
      );

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      expect(screen.getByLabelText("Loading session detail")).toBeDefined();
    });

    it("renders navigation links", async () => {
      vi.mocked(paperApi.getSession).mockResolvedValue({ ok: true, data: sessionMock } as any);

      renderWithParams(<PaperSessions />, "/paper/sessions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Deployment Detail")).toBeDefined();
      });
      expect(screen.getAllByText("Events").length).toBeGreaterThan(0);
      expect(screen.getByText("Risk & Health")).toBeDefined();
      expect(screen.getByText("Reports")).toBeDefined();
    });
  });

  describe("PaperPositions", () => {
    const positionsMock = {
      positions: {
        open_position: null,
        is_flat: true,
      },
      schema_version: 1,
    };

    const openPositionMock = {
      positions: {
        open_position: {
          symbol: "NSE:SBIN",
          quantity: 100,
          side: "long",
          entry_price: 450.5,
          current_price: 455.75,
          unrealized_pnl: 525,
          position_value: 45575,
        },
        is_flat: false,
      },
      schema_version: 1,
    };

    beforeEach(() => {
      vi.mocked(paperApi.getDeployment).mockResolvedValue({ ok: true, data: { deployment: mockDeployments.deployments[0], schema_version: 1 } } as any);
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: true, data: positionsMock } as any);
    });

    it("renders flat position state with deployment context", async () => {
      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText(/No open position/)).toBeDefined();
      });
      expect(screen.getByText("strat-1")).toBeDefined();
      expect(screen.getAllByText(/NSE:SBIN/).length).toBeGreaterThan(0);
      expect(screen.getAllByText("PAPER").length).toBeGreaterThan(0);
    });

    it("renders open position with metrics", async () => {
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: true, data: openPositionMock } as any);

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Position Detail")).toBeDefined();
      });
      expect(screen.getAllByText(/NSE:SBIN/).length).toBeGreaterThan(0);
      expect(screen.getAllByText("LONG").length).toBeGreaterThan(0);
      expect(screen.getAllByText(/45,?575/).length).toBeGreaterThan(0);
    });

    it("shows positive P&L in positive tone", async () => {
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: true, data: openPositionMock } as any);

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Position Detail")).toBeDefined();
      });
      expect(screen.getAllByText(/525/).length).toBeGreaterThan(0);
    });

    it("shows negative P&L in negative tone", async () => {
      const lossMock = {
        ...openPositionMock,
        positions: {
          ...openPositionMock.positions,
          open_position: {
            ...openPositionMock.positions.open_position,
            unrealized_pnl: -300,
          },
          is_flat: false,
        },
      };
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: true, data: lossMock } as any);

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Position Detail")).toBeDefined();
      });
      expect(screen.getAllByText(/300/).length).toBeGreaterThan(0);
    });

    it("renders navigation links", async () => {
      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Deployment Detail")).toBeDefined();
      });
      expect(screen.getByText("Session")).toBeDefined();
      expect(screen.getByText("Events")).toBeDefined();
      expect(screen.getByText("Risk & Health")).toBeDefined();
    });

    it("shows error state with retry", async () => {
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: false, error: { code: "network_error", message: "Failed" }, status: 0 } as any);

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Unable to load positions")).toBeDefined();
      });
      expect(screen.getByText("Failed")).toBeDefined();
    });

    it("shows loading skeleton", async () => {
      vi.mocked(paperApi.getPositions).mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, data: positionsMock } as any), 200))
      );

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      expect(screen.getByLabelText("Loading positions")).toBeDefined();
    });

    it("does not contain buy/sell/order controls", async () => {
      vi.mocked(paperApi.getPositions).mockResolvedValue({ ok: true, data: openPositionMock } as any);

      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Position Detail")).toBeDefined();
      });
      expect(screen.queryByText("Buy")).toBeNull();
      expect(screen.queryByText("Sell")).toBeNull();
      expect(screen.queryByText("Close")).toBeNull();
    });

    it("does not contain broker or credential UI", async () => {
      renderWithParams(<PaperPositions />, "/paper/positions/dep-1");

      await waitFor(() => {
        expect(screen.getByText(/No open position/)).toBeDefined();
      });
      expect(screen.queryByText(/API Key/i)).toBeNull();
      expect(screen.queryByText(/Secret/i)).toBeNull();
      expect(screen.queryByText(/Broker/i)).toBeNull();
    });
  });

  describe("PaperEvents", () => {
    it("renders events list with filters", async () => {
      vi.mocked(paperApi.getEvents).mockResolvedValue({
        ok: true,
        data: {
          events: {
            total_events: 2,
            last_event_sequence: 1,
            last_event_type: "bar_processed",
            last_event_timestamp: "2024-01-01T00:00:00Z",
            recent: [
              { sequence: 1, timestamp: "2024-01-01T00:00:00Z", event_type: "bar_processed", deployment_id: "dep-1", strategy_id: "strat-1", message: "Processed bar" },
            ],
          },
          schema_version: 1,
        },
      } as any);

      renderWithParams(<PaperEvents />, "/paper/events/dep-1");

      await waitFor(() => {
        expect(screen.getByText(/Processed bar/)).toBeDefined();
      });
    });
  });

  describe("PaperRiskHealth", () => {
    it("renders health and risk blocks", async () => {
      vi.mocked(paperApi.getHealth).mockResolvedValue({ ok: true, data: { health: { status: "healthy" as HealthStatus, halt_reason: null, warnings: [] }, schema_version: 1 } } as any);
      vi.mocked(paperApi.getRisk).mockResolvedValue({ ok: true, data: { risk: { decision: "allow" as RiskDecision, reason: null }, schema_version: 1 } } as any);
      vi.mocked(paperApi.getCircuitBreaker).mockResolvedValue({ ok: true, data: { circuit_breaker: { state: "closed" as CircuitState, reason: null, trip_count: 0 }, schema_version: 1 } } as any);

      renderWithParams(<PaperRiskHealth />, "/paper/risk/dep-1");

      await waitFor(() => {
        expect(screen.getAllByText("HEALTHY").length).toBeGreaterThan(0);
      });
      expect(screen.getAllByText("ALLOW").length).toBeGreaterThan(0);
    });
  });

  describe("PaperReports", () => {
    it("renders JSON export", async () => {
      vi.mocked(paperApi.exportJson).mockResolvedValue({ ok: true, data: { dashboard_snapshot: mockSnapshot, report: {} } } as any);

      renderWithParams(<PaperReports />, "/paper/reports/dep-1");

      await waitFor(() => {
        expect(screen.getByText(/Reports & Export/)).toBeDefined();
      });
    });
  });

  describe("PaperStrategies", () => {
    const strategyMockSnapshot = {
      ...mockSnapshot,
      strategy: {
        strategy_id: "strat-1",
        spec_hash: "hash-1",
        symbol: "NSE:SBIN",
        timeframe: "1d",
        lifecycle_status: "validated",
        generated_by: "test",
        name: "Test Strategy",
        description: "",
        parent_strategy_id: "",
        latest_evidence_at: "2024-01-01T00:00:00Z",
        latest_research_evidence_id: "res-1",
        latest_walk_forward_evidence_id: "wf-1",
        latest_paper_evidence_id: null,
      },
    };

    const mockEvidence = {
      evidence: {
        research_count: 2,
        walk_forward_count: 1,
        paper_trading_count: 0,
        latest_research_evidence_id: "res-2",
        latest_walk_forward_evidence_id: "wf-1",
        latest_paper_trading_evidence_id: null,
        latest_research_at: "2024-01-02T00:00:00Z",
        latest_walk_forward_at: "2024-01-01T00:00:00Z",
        latest_paper_trading_at: null,
      },
      schema_version: 1,
    };

    it("renders strategy header when dashboard loads", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: strategyMockSnapshot } as any);
      vi.mocked(paperApi.getEvidence).mockResolvedValue({ ok: true, data: mockEvidence } as any);

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      await waitFor(() => {
        expect(screen.getByText("Test Strategy")).toBeDefined();
      });
      expect(screen.getByText(/strat-1/)).toBeDefined();
      expect(screen.getByText("NSE:SBIN")).toBeDefined();
      expect(screen.getByText("1d")).toBeDefined();
      expect(screen.getByText("VALIDATED")).toBeDefined();
    });

    it("renders evidence progression", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: strategyMockSnapshot } as any);
      vi.mocked(paperApi.getEvidence).mockResolvedValue({ ok: true, data: mockEvidence } as any);

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      await waitFor(() => {
        expect(screen.getByText("Evidence Progression")).toBeDefined();
      });
      expect(screen.getByText("Research")).toBeDefined();
      expect(screen.getByText("Walk Forward")).toBeDefined();
      expect(screen.getByText("Paper Trading")).toBeDefined();
      expect(screen.getByText("2")).toBeDefined();
      expect(screen.getAllByText("1").length).toBeGreaterThan(0);
    });

    it("renders deployments table", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: strategyMockSnapshot } as any);
      vi.mocked(paperApi.getEvidence).mockResolvedValue({ ok: true, data: mockEvidence } as any);

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      await waitFor(() => {
        expect(screen.getByRole("heading", { name: "Deployments" })).toBeDefined();
      });
      expect(screen.getByText("dep-1")).toBeDefined();
    });

    it("shows empty state when no deployments", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: { deployments: [], count: 0, schema_version: 1 } } as any);

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      await waitFor(() => {
        expect(screen.getByText("No strategy data")).toBeDefined();
      });
    });

    it("shows error on API failure", async () => {
      vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: false, error: { code: "network_error", message: "Failed" }, status: 0 } as any);

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      await waitFor(() => {
        expect(screen.getByText("Unable to load strategy")).toBeDefined();
      });
      expect(screen.getByText("Failed")).toBeDefined();
    });

    it("shows loading skeleton", async () => {
      vi.mocked(paperApi.listDeployments).mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, data: mockDeployments } as any), 200))
      );

      renderWithParams(<PaperStrategies />, "/paper/strategies/strat-1", ":strategyId");

      expect(screen.getByLabelText("Loading strategy detail")).toBeDefined();
    });
  });

  describe("Safety", () => {
    it("does not contain live trading labels", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.getByText("Paper Trading")).toBeDefined();
      expect(screen.getByText(/Autonomous simulated execution/)).toBeDefined();
    });

    it("does not contain buy/sell controls", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.queryByText("Buy")).toBeNull();
      expect(screen.queryByText("Sell")).toBeNull();
    });

    it("does not contain credential fields", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.queryByText(/API Key/i)).toBeNull();
      expect(screen.queryByText(/Secret/i)).toBeNull();
      expect(screen.queryByText(/Password/i)).toBeNull();
    });

    it("does not contain broker connection controls", () => {
      renderWithRouter(<PaperTradingPage />);
      expect(screen.queryByText(/Connect/i)).toBeNull();
      expect(screen.queryByText(/Broker/i)).toBeNull();
      expect(screen.queryByText(/Upstox/i)).toBeNull();
      expect(screen.queryByText(/Zerodha/i)).toBeNull();
    });
  });

  describe("PaperTrading terminal header", () => {
    it("renders deployment context when dashboard resolves", async () => {
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: mockSnapshot } as any);

      renderWithParams(<PaperTradingPage />, "/paper/overview/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Paper Trading")).toBeDefined();
      });
      await waitFor(() => {
        expect(screen.getByText("AETHER // AUTONOMOUS TERMINAL")).toBeDefined();
        expect(screen.getByText("Live Paper")).toBeDefined();
      });
    });

    it("switches to halted state when dashboard reports halt", async () => {
      const halted = {
        ...mockSnapshot,
        health: { status: "halted" as HealthStatus, halt_reason: "Drawdown limit exceeded", warnings: [] },
      };
      vi.mocked(paperApi.getDashboard).mockResolvedValue({ ok: true, data: halted } as any);

      renderWithParams(<PaperTradingPage />, "/paper/overview/dep-1");

      await waitFor(() => {
        expect(screen.getByText("Paper Halted")).toBeDefined();
      });
    });
  });
});
