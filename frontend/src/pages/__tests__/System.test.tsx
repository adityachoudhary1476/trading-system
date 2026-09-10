import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import React from "react";
import { SystemPage } from "../System";
import { AppProvider } from "@/store/AppContext";

const mockGetPipeline = vi.fn(() => Promise.resolve([]));

vi.mock("@/data/MarketDataSource", () => ({
  dataSource: {
    mode: "mock",
    getPipeline: () => Promise.resolve([]),
  },
}));

vi.mock("@/lib/upstox", () => ({
  fetchConnectionStatus: vi.fn(),
}));

function renderWithProvider(ui: React.ReactElement) {
  return render(<AppProvider>{ui}</AppProvider>);
}

describe("SystemPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders environment panels", async () => {
    renderWithProvider(<SystemPage />);
    expect(screen.getAllByText("Environment").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Data Source").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Paper Execution").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Live Execution").length).toBeGreaterThanOrEqual(1);
  });

  it("renders Upstox Connection panel", async () => {
    renderWithProvider(<SystemPage />);
    expect(screen.getByText("Upstox Connection")).toBeDefined();
  });

  it("renders Pipeline panel", async () => {
    renderWithProvider(<SystemPage />);
    expect(screen.getByText("Pipeline")).toBeDefined();
  });

  it("loads pipeline stages from dataSource", async () => {
    const mockStages = [
      { id: "service", label: "Backend Service", status: "ready", lastActivity: Date.now(), metric: "Running" },
      { id: "live-pipeline", label: "Live Pipeline", status: "disconnected", lastActivity: null, metric: "stopped" },
    ];
    const { dataSource } = await import("@/data/MarketDataSource");
    dataSource.getPipeline = vi.fn(() => Promise.resolve(mockStages));

    renderWithProvider(<SystemPage />);
    await waitFor(() => {
      expect(screen.getByText("Backend Service")).toBeDefined();
    });
    expect(screen.getByText("Live Pipeline")).toBeDefined();
  });

  it("shows autonomous paper pipeline stage", async () => {
    const mockStages: any[] = [];
    const { dataSource } = await import("@/data/MarketDataSource");
    dataSource.getPipeline = vi.fn(() => Promise.resolve(mockStages));

    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            autonomous_paper_pipeline: { status: "running", enabled: true, last_activity: new Date().toISOString() },
            paper_execution: { enabled: true, running: true },
            live_execution: { enabled: false },
          }),
      })
    ) as any;

    renderWithProvider(<SystemPage />);
    await waitFor(() => {
      expect(screen.getByText("Autonomous Paper Pipeline")).toBeDefined();
    });
  });

  it("shows live execution as disabled", async () => {
    renderWithProvider(<SystemPage />);
    await waitFor(() => {
      expect(screen.getAllByText("Live Execution").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText("DISABLED")).toBeDefined();
  });
});
