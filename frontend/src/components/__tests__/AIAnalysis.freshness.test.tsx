import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import { fmtTime, fmtDuration } from "@/lib/format";
import type { AIAnalysis, MarketQuote } from "@/types";
import { AIAnalysisPanel } from "@/components/ai/AIAnalysis";
import { QuoteHeader } from "@/components/market/QuoteAndMetrics";
import { marketDataStore } from "@/data/marketDataStore";

// --- Mock the data source (live quote vs AI snapshot contract) ---
// The AI panel MUST read the AI snapshot from getAIAnalysis and the live
// price from the live-quote store (usePriceDelta -> getQuote). These two
// streams stay separate — the AI is never fed the live quote.
// Factory uses vi.fn() directly (no top-level variable refs) so it is safe
// to run during hoisted static imports; we read the instances back below.
vi.mock("@/data/MarketDataSource", () => ({
  dataSource: {
    mode: "live",
    getQuote: vi.fn(),
    getAIAnalysis: vi.fn(),
    getSignals: vi.fn(),
    getFeedHealth: vi.fn(),
    getPipeline: vi.fn(),
    getMarketStatus: vi.fn(),
  },
}));

vi.mock("@/lib/supabase", () => ({
  getSupabaseClient: () => ({
    auth: { getSession: () => ({ data: { session: null }, error: null }) },
  }),
}));

const { dataSource } = await import("@/data/MarketDataSource");
const ds = dataSource as unknown as {
  getQuote: ReturnType<typeof vi.fn>;
  getAIAnalysis: ReturnType<typeof vi.fn>;
  getMarketStatus: ReturnType<typeof vi.fn>;
};

const BASE_TS = 1_700_000_000_000; // fixed, deterministic "decision time"

function makeQuote(
  price: number,
  marketTimestamp: number,
  sessionState: MarketQuote["sessionState"] = "REGULAR",
): MarketQuote {
  return {
    symbol: "NSE:SBIN",
    name: "State Bank of India",
    exchange: "NSE",
    instrumentType: "equity",
    providerSymbol: "SBIN",
    price,
    previousClose: 99,
    change: price - 99,
    changePct: 1.0,
    dayOpen: 99,
    dayHigh: Math.max(price, 101),
    dayLow: Math.min(price, 99),
    dayRange: "99 — 101",
    volume: 0,
    vwap: price,
    sessionState,
    lastUpdate: marketTimestamp,
    marketTimestamp,
    fetchedAt: marketTimestamp,
  };
}

function makeAnalysis(overrides: Partial<AIAnalysis>): AIAnalysis {
  return {
    symbol: "NSE:SBIN",
    timeframe: "5m",
    bias: "bullish",
    confidence: 0.7,
    signal: "long",
    summary: "Test summary",
    factors: [{ label: "Momentum", value: "Positive", tone: "positive" }],
    generatedAt: BASE_TS,
    model: "test-model",
    decisionPrice: 100,
    decisionTimestamp: BASE_TS,
    marketTimestamp: BASE_TS - 5_000,
    dataFreshnessMs: 5_000,
    ...overrides,
  };
}

/** A live quote whose market timestamp is "now" (stays fresh, avoids stale-loop). */
function liveQuote(price: number, sessionState: MarketQuote["sessionState"] = "REGULAR") {
  const now = Date.now();
  return makeQuote(price, now, sessionState);
}

/** Read the ".value" text of the stat row whose ".label" matches `label`. */
function statValue(label: RegExp): string {
  const labels = Array.from(document.querySelectorAll<HTMLElement>(".stat .label"));
  const match = labels.find((el) => label.test(el.textContent ?? ""));
  const valueEl = match?.closest(".stat")?.querySelector<HTMLElement>(".value");
  return valueEl ? valueEl.textContent?.trim() ?? "" : "";
}

/** Wait until the AI panel has rendered (i.e. its snapshot loaded). */
async function waitForPanel() {
  await waitFor(() => expect(screen.getByText("AI Market Intelligence")).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  marketDataStore.__resetForTests();
  // Default session = regular so the live-quote store treats ticks as live.
  ds.getMarketStatus.mockResolvedValue({
    market: "NSE",
    phase: "regular",
    serverTime: Date.now(),
    nextOpen: null,
    nextClose: null,
  });
});

afterEach(() => {
  cleanup();
  marketDataStore.stop();
});

describe("AIAnalysisPanel — freshness contract (Phase 7)", () => {
  it("Case 1: fresh market data renders price, timestamps, freshness and live delta", async () => {
    // Snapshot is recent: market data 5s old, decided 5s ago; live tick is current.
    const ai = makeAnalysis({
      decisionPrice: 100,
      decisionTimestamp: BASE_TS,
      marketTimestamp: BASE_TS - 5_000,
      dataFreshnessMs: 5_000,
    });
    ds.getAIAnalysis.mockResolvedValue(ai);
    ds.getQuote.mockImplementation(async () => liveQuote(105)); // live tick = 105

    render(<AIAnalysisPanel symbol="NSE:SBIN" />);
    await waitForPanel();

    // Wait for the snapshot to load and the live delta to resolve (105 - 100).
    await waitFor(() => expect(statValue(/Data Freshness/)).toBe(fmtDuration(5_000)));

    // AI panel displays decision price (the snapshot price, not the live tick).
    expect(statValue(/Decision Price/)).toBe("₹100.00");
    // Based-on timestamp is the market snapshot timestamp.
    expect(statValue(/Based On/)).toBe(fmtTime(BASE_TS - 5_000));
    // Generation timestamp is the AI decision time.
    expect(statValue(/Generated/)).toBe(fmtTime(BASE_TS));
    // Data freshness is represented from the snapshot's dataFreshnessMs.
    expect(statValue(/Data Freshness/)).toBe(fmtDuration(5_000));
    // Live delta = live price (105) - decision price (100) = 5 (= +5.00%)
    expect(statValue(/Live/)).toContain("₹5.00");
    expect(statValue(/Live/)).toMatch(/\+5\.00%/);

    // The two streams stay separate: AI from the OHLCV snapshot, live from the quote.
    expect(ds.getAIAnalysis).toHaveBeenCalledTimes(1);
    expect(ds.getQuote).toHaveBeenCalled();
  });

  it("Case 2: stale AI snapshot does not imply the decision came from the live price", async () => {
    const snapshotAt = BASE_TS - 300_000; // decision snapshot is 5m old
    const ai = makeAnalysis({
      decisionPrice: 100,
      decisionTimestamp: snapshotAt,
      marketTimestamp: snapshotAt,
      dataFreshnessMs: 0,
    });
    ds.getAIAnalysis.mockResolvedValue(ai);
    // Live tick is NEWER than the AI decision snapshot.
    ds.getQuote.mockImplementation(async () => liveQuote(110));

    render(<AIAnalysisPanel symbol="NSE:SBIN" />);
    await waitForPanel();

    // Decision price remains the historical snapshot price (NOT the live 110).
    expect(statValue(/Decision Price/)).toBe("₹100.00");
    expect(statValue(/Decision Price/)).not.toContain("110");
    // Based-On (snapshot candle) and Generated (decision time) predate the live tick.
    expect(statValue(/Based On/)).toBe(fmtTime(snapshotAt));
    expect(statValue(/Generated/)).toBe(fmtTime(snapshotAt));
    expect(statValue(/Data Freshness/)).toBe(fmtDuration(0));
    // Live delta is live price minus the *snapshot* decision price, not live-vs-live.
    const delta = statValue(/Live/);
    expect(delta).toContain("₹10.00"); // 110 - 100 = 10
    expect(delta).toMatch(/\+10\.00%/);
    // Freshness stays accurate: the snapshot was 5m old at decision time.
    expect(BASE_TS - snapshotAt).toBe(300_000);
  });

  it("Case 3: closed market — no false implication of a live candle", async () => {
    const prevSession = BASE_TS - 86_400_000; // previous session close
    ds.getMarketStatus.mockResolvedValue({
      market: "NSE",
      phase: "closed",
      serverTime: BASE_TS,
      nextOpen: BASE_TS + 6 * 3_600_000,
      nextClose: null,
    });
    const ai = makeAnalysis({
      decisionPrice: 100,
      decisionTimestamp: prevSession,
      marketTimestamp: prevSession,
      dataFreshnessMs: 0,
    });
    ds.getAIAnalysis.mockResolvedValue(ai);
    // Last known tick is from the previous (closed) session.
    ds.getQuote.mockResolvedValue(makeQuote(108, prevSession, "CLOSED"));

    render(
      <>
        <QuoteHeader symbol="NSE:SBIN" />
        <AIAnalysisPanel symbol="NSE:SBIN" />
      </>,
    );
    await waitForPanel();

    // The live side knows the market is closed.
    await waitFor(() => {
      const live = marketDataStore.getLiveMarketState("NSE:SBIN");
      expect(live?.marketStatus).toBe("CLOSED");
      expect(live?.isLive).toBe(false);
    });

    // QuoteHeader reflects the closed session rather than "Updated Xs ago".
    const freshnessBadge = screen.getByTestId("freshness");
    expect(freshnessBadge.textContent).toMatch(/Market closed/i);

    // AI panel timestamps reference the previous-session candle, not a live tick.
    expect(statValue(/Decision Price/)).toBe("₹100.00");
    expect(statValue(/Based On/)).toBe(fmtTime(prevSession));
    expect(statValue(/Generated/)).toBe(fmtTime(prevSession));

    // The panel must not imply the analysis rides a live tick.
    const title = screen.getByText("AI Market Intelligence");
    const panelText = title.closest(".panel")?.textContent ?? "";
    expect(panelText).toMatch(/closed candles/i);
    expect(panelText).toMatch(/candle close/i);
  });

  it("Case 4: missing optional snapshot fields render '—' without crashing", async () => {
    const ai = makeAnalysis({
      decisionPrice: 100, // present so the snapshot section renders
      decisionTimestamp: null,
      marketTimestamp: null,
      dataFreshnessMs: null,
    });
    ds.getAIAnalysis.mockResolvedValue(ai);
    ds.getQuote.mockImplementation(async () => liveQuote(105));

    const { container } = render(<AIAnalysisPanel symbol="NSE:SBIN" />);
    await waitForPanel();

    // No crash, no NaN/Infinity, no misleading values.
    expect(container).toBeTruthy();
    const html = container.textContent ?? "";
    expect(html).not.toMatch(/NaN/);
    expect(html).not.toMatch(/Infinity/);
    // Decision price still renders from the snapshot.
    expect(statValue(/Decision Price/)).toBe("₹100.00");
    // Optional fields degrade to em-dash.
    expect(statValue(/Based On/)).toBe("—");
    expect(statValue(/Generated/)).toBe("—");
    expect(statValue(/Data Freshness/)).toBe("—");
    // Live delta still computes from snapshot price vs live price.
    expect(statValue(/Live/)).toContain("₹5.00");
  });

  it("Case 4b: null decisionPrice omits the snapshot section (no crash)", async () => {
    const ai = makeAnalysis({
      decisionPrice: null,
      decisionTimestamp: null,
      marketTimestamp: null,
      dataFreshnessMs: null,
    });
    ds.getAIAnalysis.mockResolvedValue(ai);
    ds.getQuote.mockImplementation(async () => liveQuote(105));

    const { container } = render(<AIAnalysisPanel symbol="NSE:SBIN" />);
    await waitForPanel();

    expect(container).toBeTruthy();
    // The Decision Snapshot section is gated on decisionPrice != null.
    expect(screen.queryByText(/Decision Snapshot/i)).toBeNull();
    expect(container.textContent ?? "").not.toMatch(/NaN/);
    expect(container.textContent ?? "").not.toMatch(/Infinity/);
  });
});
