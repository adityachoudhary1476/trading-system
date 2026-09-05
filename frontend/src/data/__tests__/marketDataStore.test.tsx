import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, render, screen, cleanup } from "@testing-library/react";
import { marketDataStore, LIVE_QUOTE_POLL_INTERVAL_MS, DEFAULT_POLL_INTERVAL_MS } from "../marketDataStore";
import { useQuote, useMarketStatus, usePriceDelta, useLiveMarketState } from "../useQuote";
import { QuoteHeader } from "@/components/market/QuoteAndMetrics";
import type { MarketQuote, MarketStatus } from "@/types";

vi.mock("../MarketDataSource", () => {
  const baseQuote: MarketQuote = {
    symbol: "NSE:SBIN",
    name: "State Bank of India",
    exchange: "NSE",
    instrumentType: "equity",
    providerSymbol: "SBIN",
    price: 850.5,
    previousClose: 840,
    change: 10.5,
    changePct: 1.25,
    dayOpen: 845,
    dayHigh: 855,
    dayLow: 840,
    dayRange: "840 — 855",
    volume: 1_500_000,
    vwap: 848.2,
    lastUpdate: 1700000000000,
    sessionState: "REGULAR",
  };
  const baseStatus: MarketStatus = {
    market: "NSE",
    phase: "regular",
    serverTime: 1700000000000,
    nextOpen: null,
    nextClose: null,
  };
  let getQuoteImpl: (s: string) => Promise<MarketQuote> = async () => baseQuote;
  let getStatusImpl: () => Promise<MarketStatus> = async () => baseStatus;
  return {
    dataSource: {
      mode: "live",
      getQuote: (s: string) => getQuoteImpl(s),
      getOHLCV: async () => [],
      getAIAnalysis: async () => ({} as any),
      getSignals: async () => [],
      getFeedHealth: async () => ({} as any),
      getPipeline: async () => [],
      getMarketStatus: () => getStatusImpl(),
    },
    __setQuoteImpl: (fn: (s: string) => Promise<MarketQuote>) => {
      getQuoteImpl = fn;
    },
    __setStatusImpl: (fn: () => Promise<MarketStatus>) => {
      getStatusImpl = fn;
    },
  };
});

const mod = await import("../MarketDataSource");
const setQuote = (mod as any).__setQuoteImpl as (fn: (s: string) => Promise<MarketQuote>) => void;
const setStatus = (mod as any).__setStatusImpl as (fn: () => Promise<MarketStatus>) => void;

beforeEach(() => {
  marketDataStore.__resetForTests();
  // 50ms poll, 100ms staleness. Short enough that real-time tests
  // finish in a few hundred ms.
  marketDataStore.configure({ pollIntervalMs: 50, staleAfterMs: 100 });
});

afterEach(() => {
  cleanup();
  marketDataStore.stop();
  vi.useRealTimers();
  // Reset mock impls so the next test starts clean.
  setQuote(async (s) => makeQuote(100, s));
  setStatus(async () => ({
    market: "NSE",
    phase: "regular",
    serverTime: 1700000000000,
    nextOpen: null,
    nextClose: null,
  }));
});

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

function makeQuote(price: number, symbol = "NSE:SBIN"): MarketQuote {
  return {
    symbol,
    name: symbol,
    exchange: "NSE",
    instrumentType: "equity",
    providerSymbol: symbol,
    price,
    previousClose: 99,
    change: price - 99,
    changePct: 1.01,
    dayOpen: 99,
    dayHigh: Math.max(price, 101),
    dayLow: Math.min(price, 99),
    dayRange: "99 — 101",
    volume: 0,
    vwap: price,
    lastUpdate: 1700000000000,
    sessionState: "REGULAR",
  };
}

describe("marketDataStore", () => {
  it("returns the same empty snapshot reference until state changes", () => {
    const first = marketDataStore.getState("NSE:SBIN");
    const second = marketDataStore.getState("NSE:SBIN");
    expect(second).toBe(first);
  });

  it("subscribing triggers an immediate fetch and demotes to 'fresh'", async () => {
    setQuote(async (s) => makeQuote(850, s));
    const listener = vi.fn();
    const unsub = marketDataStore.subscribe("NSE:SBIN", listener);
    await wait(30);
    expect(listener).toHaveBeenCalled();
    const last = listener.mock.calls.at(-1)?.[0];
    expect(last?.freshness).toBe("fresh");
    expect(last?.data?.price).toBe(850);
    unsub();
  });

  it("preserves the last valid price when a fetch fails", async () => {
    const listener = vi.fn();
    let mode: "ok" | "fail" = "ok";
    setQuote(async (s) => {
      if (mode === "fail") throw new Error("network down");
      return makeQuote(100, s);
    });
    const unsub = marketDataStore.subscribe("NSE:SBIN", listener);
    await wait(30);
    expect(marketDataStore.getState("NSE:SBIN").data?.price).toBe(100);
    mode = "fail";
    await wait(300);
    const state = marketDataStore.getState("NSE:SBIN");
    expect(state.data?.price).toBe(100);
    expect(state.freshness).toBe("error");
    expect(state.error).toMatch(/network down/);
    unsub();
  });

  it("demotes 'fresh' to 'stale' after staleAfterMs elapses", async () => {
    setQuote(async (s) => makeQuote(100, s));
    const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
    await wait(30);
    expect(marketDataStore.getState("NSE:SBIN").freshness).toBe("fresh");
    // Stop polling by unsusbscribing; the cached state should drift to
    // 'stale' once 100ms has passed since the last successful fetch.
    unsub();
    await wait(300);
    expect(marketDataStore.getState("NSE:SBIN").freshness).toBe("stale");
  });

  it("overlap-prevention: a single inflight fetch is shared across ticks", async () => {
    let calls = 0;
    let resolveFn: ((v: MarketQuote) => void) | null = null;
    setQuote((s) => {
      calls += 1;
      return new Promise<MarketQuote>((resolve) => {
        resolveFn = () => resolve(makeQuote(100, s));
      });
    });
    const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
    await wait(20);
    // Allow multiple poll ticks while inflight is still pending.
    await wait(200);
    expect(calls).toBe(1);
    if (resolveFn) resolveFn(makeQuote(100, "NSE:SBIN"));
    await wait(200);
    expect(calls).toBeGreaterThanOrEqual(2);
    unsub();
  });

  it("timer is cleared after the last unsubscribe", async () => {
    const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
    await wait(30);
    const before = marketDataStore.getState("NSE:SBIN");
    unsub();
    await wait(200);
    const after = marketDataStore.getState("NSE:SBIN");
    expect(after.lastSuccessTs).toBe(before.lastSuccessTs);
  });

  it("marks freshness as 'closed' when the server reports the market is closed", async () => {
    setStatus(async () => ({
      market: "NSE",
      phase: "closed",
      serverTime: Date.now(),
      nextOpen: Date.now() + 60_000,
      nextClose: null,
    }));
    setQuote(async (s) => makeQuote(100, s));
    const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
    const unsubS = marketDataStore.subscribeSession(() => {});
    await wait(60);
    const state = marketDataStore.getState("NSE:SBIN");
    expect(state.freshness).toBe("closed");
    expect(marketDataStore.getSession()?.phase).toBe("closed");
    unsubQ();
    unsubS();
  });
});

describe("useQuote hook", () => {
  it("returns a fresh quote and updates when the store updates", async () => {
    let n = 0;
    setQuote(async (s) => makeQuote(100 + n, s));
    const { result } = renderHook(() => useQuote("NSE:SBIN"));
    await wait(30);
    expect(result.current.data?.price).toBe(100);
    n = 5;
    await wait(200);
    expect(result.current.data?.price).toBe(105);
  });
});

describe("useMarketStatus hook", () => {
  it("returns the current session state", async () => {
    setStatus(async () => ({
      market: "NSE",
      phase: "regular",
      serverTime: 1700000000000,
      nextOpen: null,
      nextClose: null,
    }));
    const { result } = renderHook(() => useMarketStatus());
    await wait(60);
    expect(result.current?.phase).toBe("regular");
  });
});

describe("QuoteHeader freshness badge", () => {
  it("renders a 'fresh' badge once a quote arrives", async () => {
    setQuote(async (s) => makeQuote(100, s));
    render(<QuoteHeader symbol="NSE:SBIN" />);
    await wait(60);
    const badge = screen.getByTestId("freshness");
    expect(badge.textContent).toMatch(/Updated/);
    // The freshness class lives on the parent div (.freshness)
    expect(badge.parentElement?.classList.contains("fresh")).toBe(true);
  });

  it("renders 'stale' when no recent successful fetch", async () => {
    setQuote(async (s) => makeQuote(100, s));
    render(<QuoteHeader symbol="NSE:SBIN" />);
    await wait(60);
    const before = marketDataStore.getState("NSE:SBIN");
    expect(before.freshness).toBe("fresh");
    // Stop polling entirely; with no new successful fetches, the
    // clock tick will eventually demote to stale.
    marketDataStore.stop();
    await wait(300);
    const after = marketDataStore.getState("NSE:SBIN");
    expect(after.freshness).toBe("stale");
  });

  it("renders 'closed' when the session is closed", async () => {
    setStatus(async () => ({
      market: "NSE",
      phase: "closed",
      serverTime: 1700000000000,
      nextOpen: null,
      nextClose: null,
    }));
    setQuote(async (s) => makeQuote(100, s));
    render(<QuoteHeader symbol="NSE:SBIN" />);
    await wait(60);
    const badge = screen.getByTestId("freshness");
    expect(badge.parentElement?.classList.contains("closed")).toBe(true);
    expect(badge.textContent).toMatch(/Market closed/);
  });

  it("renders 'Data temporarily unavailable' on the first failed fetch", async () => {
    setQuote(async () => {
      throw new Error("boom");
    });
    render(<QuoteHeader symbol="NSE:SBIN" />);
    await wait(60);
    // First failed fetch with no prior valid quote falls through to
    // the unavailable branch instead of a freshness badge.
    expect(screen.queryByText(/Data temporarily unavailable/)).not.toBeNull();
  });
});

describe("marketTimestamp freshness (Phase 2)", () => {
  it("uses marketTimestamp (exchange tick time) instead of fetch time for staleness", async () => {
    const oldTs = Date.now() - 10_000; // 10s ago — older than staleAfterMs (100)
    setQuote(async (s) => ({ ...makeQuote(100, s), marketTimestamp: oldTs, lastUpdate: oldTs, fetchedAt: Date.now() }));
    const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
    await wait(60);
    const state = marketDataStore.getState("NSE:SBIN");
    expect(state.freshness).toBe("stale");
    expect(state.marketTimestamp).toBe(oldTs);
    unsub();
  });

  it("keeps a quote fresh when marketTimestamp is current", async () => {
    const now = Date.now();
    setQuote(async (s) => ({ ...makeQuote(100, s), marketTimestamp: now, lastUpdate: now, fetchedAt: now }));
    const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
    await wait(30);
    const state = marketDataStore.getState("NSE:SBIN");
    expect(state.freshness).toBe("fresh");
    expect(state.marketTimestamp).toBe(now);
    unsub();
  });

  it("getLiveMarketState returns canonical LiveMarketState with isStale=true for old marketTimestamp", async () => {
    const oldTs = Date.now() - 10_000;
    setQuote(async (s) => ({ ...makeQuote(100, s), marketTimestamp: oldTs, lastUpdate: oldTs, fetchedAt: Date.now() }));
    const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
    const unsubS = marketDataStore.subscribeSession(() => {});
    await wait(80);
    const live = marketDataStore.getLiveMarketState("NSE:SBIN");
    expect(live).not.toBeNull();
    expect(live?.symbol).toBe("NSE:SBIN");
    expect(live?.price).toBe(100);
    expect(live?.marketTimestamp).toBe(oldTs);
    expect(live?.isStale).toBe(true);
    expect(live?.marketStatus).toBe("OPEN");
    unsubQ();
    unsubS();
  });

  it("getLiveMarketState returns null when no quote fetched yet", () => {
    expect(marketDataStore.getLiveMarketState("NSE:NONEXISTENT")).toBeNull();
  });

  it("subscribeLive emits updated LiveMarketState on quote change", async () => {
    setQuote(async (s) => makeQuote(100, s));
    const listener = vi.fn();
    const unsub = marketDataStore.subscribeLive("NSE:SBIN", listener);
    await wait(80);
    const lastCall = listener.mock.calls.at(-1);
    expect(lastCall?.[0]).not.toBeNull();
    expect(lastCall?.[0]?.price).toBe(100);
    unsub();
  });

  it("keeps the derived live snapshot stable when no state changes", async () => {
    setQuote(async (s) => makeQuote(100, s));
    const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
    const unsubS = marketDataStore.subscribeSession(() => {});
    await wait(60);
    const first = marketDataStore.getLiveMarketState("NSE:SBIN");
    const second = marketDataStore.getLiveMarketState("NSE:SBIN");
    expect(second).toBe(first);
    unsubQ();
    unsubS();
  });
});

describe("usePriceDelta hook (Phase 5)", () => {
  it("returns null when decisionPrice is null", () => {
    setQuote(async (s) => makeQuote(100, s));
    const { result } = renderHook(() => usePriceDelta("NSE:SBIN", null));
    expect(result.current).toBeNull();
  });

  it("returns the difference between live price and decisionPrice", async () => {
    setQuote(async (s) => makeQuote(105, s));
    const { result } = renderHook(() => usePriceDelta("NSE:SBIN", 100));
    await wait(60);
    expect(result.current).toBe(5);
  });

   it("returns null when quote data is not yet available", () => {
     const { result } = renderHook(() => usePriceDelta("NSE:NONEXISTENT", 100));
     expect(result.current).toBeNull();
   });
});

// ---------------------------------------------------------------------------
// Phase 21 — 0.5 Hz (2 s) live quote polling contract
// ---------------------------------------------------------------------------

describe("live quote poll interval (0.5 Hz)", () => {
   it("LIVE_QUOTE_POLL_INTERVAL_MS is 2000 (0.5 Hz)", () => {
     expect(LIVE_QUOTE_POLL_INTERVAL_MS).toBe(2000);
     expect(DEFAULT_POLL_INTERVAL_MS).toBe(2000);
   });

   it("does not poll faster than once every 2 seconds by default", () => {
     // The default (unconfigured) interval must never be 1000 ms.
     expect(LIVE_QUOTE_POLL_INTERVAL_MS).not.toBe(1000);
     expect(LIVE_QUOTE_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(2000);
   });
});

describe("deduplication & cleanup (0.5 Hz contract)", () => {
   it("multiple subscribers share a single polling lifecycle", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub1 = marketDataStore.subscribe("NSE:SBIN", () => {});
     const unsub2 = marketDataStore.subscribe("NSE:SBIN", () => {});
     const unsub3 = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);

     // Three subscribers → exactly ONE initial fetch (dedup).
     expect(calls).toBe(1);

     unsub1();
     // Still two subscribers — timer keeps running, next tick at 50 ms.
     await wait(60);
     expect(calls).toBe(2); // 1 initial + 1 timer tick

     unsub2();
     unsub3();
   });

   it("timer is cleared after the last unsubscribe", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);
     const before = calls;
     unsub();

     // Wait well past a timer tick — no new fetches should occur.
     await wait(150);
     expect(calls).toBe(before);
   });
});

describe("overlap prevention (2 s semantics)", () => {
   it("a slow in-flight fetch is not duplicated by the next tick", async () => {
     let calls = 0;
     let resolveFn: ((v: MarketQuote) => void) | null = null;
     setQuote((s) => {
       calls += 1;
       return new Promise<MarketQuote>((resolve) => {
         resolveFn = () => resolve(makeQuote(100, s));
       });
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);

     // Allow multiple poll ticks while the fetch is still pending.
     // With a 50 ms test interval, several ticks fire but only ONE
     // actual HTTP call is made (inflight dedup).
     await wait(200);
     expect(calls).toBe(1);

     if (resolveFn) resolveFn(makeQuote(100, "NSE:SBIN"));
     await wait(200);
     expect(calls).toBeGreaterThanOrEqual(2);
     unsub();
   });
});

describe("recovery (tab visibility / network)", () => {
   let originalHidden: boolean;

   beforeEach(() => {
     originalHidden = document.hidden;
   });

   afterEach(() => {
     Object.defineProperty(document, "hidden", {
       value: originalHidden,
       configurable: true,
     });
   });

   it("refreshNow triggers an immediate fetch for active subscribers", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);
     expect(calls).toBe(1); // initial fetch only — 50 ms interval hasn't ticked yet

     marketDataStore.refreshNow();
     await wait(20);
     // At least one extra fetch beyond the initial one (immediate on-demand).
     expect(calls).toBeGreaterThanOrEqual(2);

     unsub();
   });

   it("pauseAllQuotePolling when tab hidden; resume + immediate fetch on visible", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);
     const baseline = calls;

     // Hide the tab — the visibilitychange handler should pause polling.
     Object.defineProperty(document, "hidden", { value: true, configurable: true });
     document.dispatchEvent(new Event("visibilitychange"));
     await wait(150); // enough for 2-3 timer ticks at 50 ms

     // No new fetches while hidden.
     expect(calls).toBe(baseline);

     // Show the tab — immediate fetch + timer resumes.
     Object.defineProperty(document, "hidden", { value: false, configurable: true });
     document.dispatchEvent(new Event("visibilitychange"));
     await wait(20);
     expect(calls).toBeGreaterThanOrEqual(baseline + 1);

     unsub();
   });

   it("online event triggers immediate refresh", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);
     const baseline = calls;

     window.dispatchEvent(new Event("online"));
     await wait(20);
     expect(calls).toBeGreaterThanOrEqual(baseline + 1);

     unsub();
   });

   it("refreshNow is a no-op when the tab is hidden", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     const unsub = marketDataStore.subscribe("NSE:SBIN", () => {});
     await wait(20);

     Object.defineProperty(document, "hidden", { value: true, configurable: true });
     document.dispatchEvent(new Event("visibilitychange"));
     await wait(20);
     const hiddenCalls = calls;

     marketDataStore.refreshNow();
     await wait(20);
     expect(calls).toBe(hiddenCalls); // no fetch while hidden

     unsub();
   });
});

describe("market-closed gating", () => {
   it("pauses quote polling when the market is closed", async () => {
     let calls = 0;
     setQuote(async (s) => {
       calls += 1;
       return makeQuote(100, s);
     });
     setStatus(async () => ({
       market: "NSE",
       phase: "closed",
       serverTime: Date.now(),
       nextOpen: Date.now() + 60_000,
       nextClose: null,
     }));

     const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
     const unsubS = marketDataStore.subscribeSession(() => {});
     await wait(60);

     // One immediate fetch (from subscribe); no timer ticks since the
     // session reported closed and paused the timer.
     expect(calls).toBe(1);

     // Wait past several timer ticks — still only 1 fetch.
     await wait(200);
     expect(calls).toBe(1);

     // State is "closed", not "fresh" — no fabricated freshness.
     expect(marketDataStore.getState("NSE:SBIN").freshness).toBe("closed");
     expect(marketDataStore.getState("NSE:SBIN").data?.price).toBe(100);

     unsubQ();
     unsubS();
   });

    it("preserves last valid quote when market transitions to closed", async () => {
      setQuote(async (s) => makeQuote(100, s));
      setStatus(async () => ({
        market: "NSE",
        phase: "regular",
        serverTime: Date.now(),
        nextOpen: null,
        nextClose: null,
      }));

      const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
      let unsubS = marketDataStore.subscribeSession(() => {});
      await wait(60);

      expect(marketDataStore.getState("NSE:SBIN").freshness).toBe("fresh");

      // Market closes — re-trigger session fetch to detect the transition.
      setStatus(async () => ({
        market: "NSE",
        phase: "closed",
        serverTime: Date.now(),
        nextOpen: Date.now() + 60_000,
        nextClose: null,
      }));
      unsubS(); // clears the 60 s session timer
      await wait(10);
      unsubS = marketDataStore.subscribeSession(() => {}); // new immediate fetch
      await wait(80);

      const state = marketDataStore.getState("NSE:SBIN");
      expect(state.freshness).toBe("closed");
      expect(state.data?.price).toBe(100); // last valid quote preserved

      unsubQ();
      unsubS();
    });

    it("resumes polling with immediate fetch when market reopens", async () => {
      let calls = 0;
      setQuote(async (s) => {
        calls += 1;
        return makeQuote(100, s);
      });
      setStatus(async () => ({
        market: "NSE",
        phase: "closed",
        serverTime: Date.now(),
        nextOpen: Date.now() + 60_000,
        nextClose: null,
      }));

      const unsubQ = marketDataStore.subscribe("NSE:SBIN", () => {});
      let unsubS = marketDataStore.subscribeSession(() => {});
      await wait(60);
      expect(calls).toBe(1); // only initial fetch while closed

      // Market reopens — re-trigger session fetch to detect the transition.
      setStatus(async () => ({
        market: "NSE",
        phase: "regular",
        serverTime: Date.now(),
        nextOpen: null,
        nextClose: null,
      }));
      unsubS(); // clears the 60 s session timer
      await wait(10);
      unsubS = marketDataStore.subscribeSession(() => {}); // new immediate fetch
      await wait(80); // immediate fetch on reopen + timer tick at 50 ms

      // Immediate fetch on reopen + at least one timer tick.
      expect(calls).toBeGreaterThanOrEqual(2);

      unsubQ();
      unsubS();
    });
});
