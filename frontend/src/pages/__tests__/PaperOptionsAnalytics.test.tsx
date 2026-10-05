import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PaperOptionsAnalytics } from "@/pages/paper/PaperOptionsAnalytics";
import { paperApi } from "@/lib/paperApi";

vi.mock("@/lib/paperApi", () => ({
  paperApi: {
    getOptionsAnalytics: vi.fn(),
    getOptionExpiries: vi.fn(),
    listDeployments: vi.fn(),
  },
}));

const mockList = {
  deployments: [
    {
      deployment_id: "dep-1",
      strategy_id: "s1",
      spec_hash: "abc",
      symbol: "NSE:SBIN",
      timeframe: "1d",
      status: "active",
      created_at: "2026-10-01T00:00:00Z",
    },
  ],
  count: 1,
};

const mockAnalytics = {
  underlying: "NIFTY",
  expiry: "2026-11-19",
  as_of: "2026-10-05T09:30:00Z",
  spot_price: 25000,
  strike_interval: 50,
  window_strikes: null,
  window_truncated: false,
  rows: [
    {
      strike: 25000,
      option_type: "CE" as const,
      instrument_key: "NSE_INDEX:NIFTYCE25000",
      ltp: 642.5,
      bid: 642,
      ask: 643,
      oi: 1000,
      change_oi: -250,
      implied_vol: 0.15,
      iv_source: "quote" as const,
      delta: 0.5231,
      gamma: 0.000296,
      theta: -3.7195,
      vega: 34.6893,
      rho: 12.3456,
      moneyness: "ATM" as const,
    },
    {
      // Same strike, other side: proves CE/PE are distinct rows.
      strike: 25000,
      option_type: "PE" as const,
      instrument_key: "NSE_INDEX:NIFTYPE25000",
      ltp: 610.25,
      bid: 609,
      ask: 611,
      oi: 2000,
      change_oi: null,
      implied_vol: 0.16,
      iv_source: "quote" as const,
      delta: -0.4769,
      gamma: 0.000296,
      theta: 1.2345,
      vega: 34.6893,
      rho: -11.1111,
      moneyness: "ATM" as const,
    },
  ],
  summary: {
    atm_strike: 25000,
    lower_strike: 24500,
    upper_strike: 25500,
    rows_total: 2,
    rows_with_greeks: 2,
    coverage: 1,
    call_delta_total: 0.5231,
    put_delta_total: -0.4769,
    net_delta: 0.0462,
    gamma_total: 0.000592,
    theta_total: -2.485,
    vega_total: 69.3786,
    call_oi: 1000,
    put_oi: 2000,
    put_call_oi_ratio: 2,
  },
  notes: ["showing 3 of 45 strikes, 1 either side of ATM 25000"],
};

/**
 * Nearest listed expiry first: the page preselects it, so the shared analytics
 * fixture uses the same date.
 */
const mockExpiries = {
  underlying: "NIFTY",
  expiries: ["2026-11-19", "2026-12-17"],
  count: 2,
  source: "provider" as const,
  notes: [],
};

/** Drives the page to a loaded state: pick a deployment, then let the page
 *  preselect the nearest listed expiry. */
async function setup() {
  render(
    <MemoryRouter>
      <PaperOptionsAnalytics />
    </MemoryRouter>,
  );

  const picker = await screen.findByLabelText("Select deployment");
  fireEvent.change(picker, { target: { value: "dep-1" } });

  await screen.findByRole("option", { name: "2026-11-19" });
}

function pickExpiry(date: string) {
  fireEvent.change(screen.getByLabelText("Expiry"), { target: { value: date } });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(paperApi.listDeployments).mockResolvedValue({
    ok: true,
    data: mockList,
  } as never);
  vi.mocked(paperApi.getOptionExpiries).mockResolvedValue({
    ok: true,
    data: mockExpiries,
  } as never);
  vi.mocked(paperApi.getOptionsAnalytics).mockResolvedValue({
    ok: true,
    data: mockAnalytics,
  } as never);
});

describe("PaperOptionsAnalytics — prerequisites", () => {
  it("does not list expiries until a deployment is picked", async () => {
    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("Select a deployment"),
    ).toBeInTheDocument();
    // Nothing to analyse until a deployment exists.
    expect(paperApi.getOptionsAnalytics).not.toHaveBeenCalled();
    expect(paperApi.getOptionExpiries).not.toHaveBeenCalled();
  });

  it("lists expiries for the chosen deployment and underlying", async () => {
    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Select deployment"), {
      target: { value: "dep-1" },
    });

    await waitFor(() =>
      expect(paperApi.getOptionExpiries).toHaveBeenCalledWith("dep-1", "NIFTY"),
    );
  });

  it("preselects the nearest listed expiry", async () => {
    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Select deployment"), {
      target: { value: "dep-1" },
    });

    // Nearest expiry first is deliberate: it is the contract a reader asking
    // for "options analytics" almost always means.
    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenCalledWith(
        "dep-1",
        "NIFTY",
        expect.objectContaining({ expiry: "2026-11-19" }),
      ),
    );
  });

  it("offers exactly the listed expiries and no invented dates", async () => {
    await setup();

    const select = screen.getByLabelText("Expiry") as HTMLSelectElement;
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual(["2026-11-19", "2026-12-17"]);
  });

  it("analyses the expiry the user picks from the list", async () => {
    await setup();

    pickExpiry("2026-12-17");

    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenLastCalledWith(
        "dep-1",
        "NIFTY",
        expect.objectContaining({ expiry: "2026-12-17" }),
      ),
    );
  });

  it("refetches expiries when the underlying changes", async () => {
    await setup();

    fireEvent.change(screen.getByLabelText("Underlying"), {
      target: { value: "BANKNIFTY" },
    });

    await waitFor(() =>
      expect(paperApi.getOptionExpiries).toHaveBeenLastCalledWith(
        "dep-1",
        "BANKNIFTY",
      ),
    );
    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenLastCalledWith(
        "dep-1",
        "BANKNIFTY",
        expect.objectContaining({ expiry: "2026-11-19" }),
      ),
    );
  });

  it("calls the API with the deployment, symbol, and expiry", async () => {
    await setup();

    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenCalledWith(
        "dep-1",
        "NIFTY",
        expect.objectContaining({ expiry: "2026-11-19" }),
      ),
    );
  });
});

describe("PaperOptionsAnalytics — expiry discovery", () => {
  it("shows an empty state instead of a broken dropdown when listing fails", async () => {
    // "The exchange has no expiries" and "we could not ask" are different, and
    // conflating them hides an auth failure behind an empty select.
    vi.mocked(paperApi.getOptionExpiries).mockResolvedValue({
      ok: true,
      data: {
        underlying: "NIFTY",
        expiries: [],
        count: 0,
        source: "unavailable",
        notes: [
          "no expiries could be listed for this underlying — the chain provider may be unauthenticated",
        ],
      },
    } as never);

    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Select deployment"), {
      target: { value: "dep-1" },
    });

    expect(await screen.findByText("No expiries available")).toBeInTheDocument();
    expect(
      screen.getByText(/the chain provider may be unauthenticated/),
    ).toBeInTheDocument();
    // With nothing to analyse, no analytics request is made.
    expect(paperApi.getOptionsAnalytics).not.toHaveBeenCalled();
  });

  it("surfaces a listing failure with a retry affordance", async () => {
    vi.mocked(paperApi.getOptionExpiries)
      .mockResolvedValueOnce({ ok: false, error: { message: "chain provider offline" } } as never)
      .mockResolvedValue({ ok: true, data: mockExpiries } as never);

    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Select deployment"), {
      target: { value: "dep-1" },
    });

    expect(await screen.findByText("Unable to load expiries")).toBeInTheDocument();
    expect(screen.getByText("chain provider offline")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() =>
      expect(paperApi.getOptionExpiries).toHaveBeenCalledTimes(2),
    );
    expect(
      await screen.findByRole("option", { name: "2026-11-19" }),
    ).toBeInTheDocument();
  });
});

describe("PaperOptionsAnalytics — rendering", () => {
  it("shows the ATM summary aggregates", async () => {
    await setup();

    expect(await screen.findByText("Summary — NIFTY 2026-11-19")).toBeInTheDocument();
    expect(screen.getByText("ATM Strike")).toBeInTheDocument();
    // "25000" also appears in the per-strike column, so match the label's
    // sibling rather than the raw number.
    expect(screen.getByText("ATM Strike").parentElement).toHaveTextContent("25000");
    expect(screen.getByText("Net Delta")).toBeInTheDocument();
    expect(screen.getByText("Net Delta").parentElement).toHaveTextContent("0.05");
    expect(screen.getByText("Put/Call OI")).toBeInTheDocument();
    expect(screen.getByText("Put/Call OI").parentElement).toHaveTextContent("2.00");
  });

  it("reports coverage alongside the totals", async () => {
    await setup();

    // The reason this string matters: a net delta computed from 2 of 40 rows
    // means something different from one computed from all 40.
    expect(
      await screen.findByText(/Greeks available for 2 of 2 rows \(100.0%\)/),
    ).toBeInTheDocument();
  });

  it("renders one row per strike and side", async () => {
    await setup();

    expect(await screen.findByText("Greeks by strike")).toBeInTheDocument();
    expect(screen.getAllByText("CE")).toHaveLength(1);
    expect(screen.getAllByText("PE")).toHaveLength(1);
    expect(screen.getAllByText("ATM")).toHaveLength(2);
  });

  it("renders implied vol as a decimal percentage", async () => {
    await setup();

    expect(await screen.findByText("15.0%")).toBeInTheDocument();
    expect(screen.getByText("16.0%")).toBeInTheDocument();
  });

  it("shows the notes returned by the backend", async () => {
    await setup();

    expect(await screen.findByText("Notes")).toBeInTheDocument();
    expect(
      screen.getByText("showing 3 of 45 strikes, 1 either side of ATM 25000"),
    ).toBeInTheDocument();
  });

  it("marks the view as observational", async () => {
    await setup();

    expect(
      await screen.findByText(/OBSERVATIONAL — this view never places/),
    ).toBeInTheDocument();
  });
});

describe("PaperOptionsAnalytics — honest unknowns", () => {
  const sparse = {
    ...mockAnalytics,
    rows: [
      {
        ...mockAnalytics.rows[0],
        implied_vol: null,
        iv_source: null,
        delta: null,
        gamma: null,
        theta: null,
        vega: null,
        rho: null,
        moneyness: null,
      },
    ],
    summary: {
      ...mockAnalytics.summary,
      rows_total: 1,
      rows_with_greeks: 0,
      coverage: 0,
      net_delta: null,
      call_delta_total: null,
    },
    notes: ["spot price is unavailable; greeks cannot be computed"],
  };

  beforeEach(() => {
    vi.mocked(paperApi.getOptionsAnalytics).mockResolvedValue({
      ok: true,
      data: sparse,
    } as never);
  });

  it("renders an uncomputed greeks cell as a dash, not 0.00", async () => {
    await setup();

    await screen.findByText("Greeks by strike");

    // A rendered 0.00 would be indistinguishable from a real zero delta.
    expect(screen.queryByText("0.0000")).not.toBeInTheDocument();
    const dashes = screen.getAllByTitle("not computable from available data");
    expect(dashes).toHaveLength(5);
  });

  it("shows zero coverage rather than implying full coverage", async () => {
    await setup();

    expect(
      await screen.findByText(/Greeks available for 0 of 1 rows \(0.0%\)/),
    ).toBeInTheDocument();
  });

  it("surfaces the backend's reason in the notes", async () => {
    await setup();

    expect(
      await screen.findByText("spot price is unavailable; greeks cannot be computed"),
    ).toBeInTheDocument();
  });
});

describe("PaperOptionsAnalytics — states", () => {
  it("surfaces an API error with a retry affordance", async () => {
    vi.mocked(paperApi.getOptionsAnalytics).mockResolvedValue({
      ok: false,
      error: { code: "not_found", message: "no option chain available for NIFTY" },
    } as never);

    await setup();

    expect(
      await screen.findByText("Unable to load options analytics"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("no option chain available for NIFTY"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("shows an empty state when the chain returns no rows", async () => {
    vi.mocked(paperApi.getOptionsAnalytics).mockResolvedValue({
      ok: true,
      data: {
        ...mockAnalytics,
        rows: [],
        notes: ["expiry 2026-11-19 is not in the future; greeks are unavailable"],
        summary: { ...mockAnalytics.summary, rows_total: 0, rows_with_greeks: 0 },
      },
    } as never);

    await setup();

    expect(await screen.findByText("No greeks available")).toBeInTheDocument();
    // The same note appears in the Notes panel and echoed in the empty-state
    // hint, so both are expected.
    expect(
      screen.getAllByText("expiry 2026-11-19 is not in the future; greeks are unavailable"),
    ).toHaveLength(2);
  });

  it("flags a clamped window", async () => {
    vi.mocked(paperApi.getOptionsAnalytics).mockResolvedValue({
      ok: true,
      data: {
        ...mockAnalytics,
        window_truncated: true,
        window_strikes: 99999,
      },
    } as never);

    await setup();

    expect(
      await screen.findByText(/window clamped to the maximum/),
    ).toBeInTheDocument();
  });
});

describe("PaperOptionsAnalytics — window control", () => {
  it("passes an explicit ATM-only window through", async () => {
    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    const picker = await screen.findByLabelText("Select deployment");
    fireEvent.change(picker, { target: { value: "dep-1" } });
    // The nearest listed expiry is already preselected, so these tests only need
    // to move the window control.
    await screen.findByRole("option", { name: "2026-11-19" });
    fireEvent.change(screen.getByLabelText("Strikes either side of ATM"), {
      target: { value: "0" },
    });

    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenLastCalledWith(
        "dep-1",
        "NIFTY",
        expect.objectContaining({ strikes: 0 }),
      ),
    );
  });

  it("omits strikes entirely when the server default is selected", async () => {
    render(
      <MemoryRouter>
        <PaperOptionsAnalytics />
      </MemoryRouter>,
    );

    const picker = await screen.findByLabelText("Select deployment");
    fireEvent.change(picker, { target: { value: "dep-1" } });
    // The nearest listed expiry is already preselected, so these tests only need
    // to move the window control.
    await screen.findByRole("option", { name: "2026-11-19" });
    fireEvent.change(screen.getByLabelText("Strikes either side of ATM"), {
      target: { value: "5" },
    });
    await waitFor(() =>
      expect(paperApi.getOptionsAnalytics).toHaveBeenLastCalledWith(
        "dep-1",
        "NIFTY",
        expect.objectContaining({ strikes: 5 }),
      ),
    );

    // 0 is a real value, so switching back to "default" must remove the key
    // entirely rather than send strikes=undefined or a stale number.
    fireEvent.change(screen.getByLabelText("Strikes either side of ATM"), {
      target: { value: "default" },
    });

    await waitFor(() => {
      const call = vi.mocked(paperApi.getOptionsAnalytics).mock.calls.at(-1);
      expect(call?.[2]).toEqual({ expiry: "2026-11-19" });
    });
  });
});