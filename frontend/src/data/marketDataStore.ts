// Centralized live-quote store.
//
// Architecture overview
// --------------------
//
// Components that need a live quote (Dashboard, Sidebar, TickerStrip,
// AIAnalysis panel, etc.) all consume this store. The store polls
// `GET /api/market/quote` on a single 0.5 Hz timer (2 s per request)
// **per active symbol**, deduping requests across components so three
// mounts of the dashboard trigger exactly one HTTP call per 2 seconds
// for the active symbol.
//
// Overlap prevention: if a previous fetch for the same symbol is still
// in flight when the next tick fires, the in-flight promise is reused
// — no parallel HTTP request is issued.
//
// Lifecycle
// ---------
//
// * On the first `subscribe(symbol, listener)` for a given symbol, the
//   store immediately fetches a quote and starts a 2-second timer.
// * On the last `unsubscribe(symbol)` for that symbol, the timer is
//   cleared.
// * On `stop()` (called from the test teardown and on tab unload), all
//   timers are cleared, global listeners are detached, and pending
//   fetches are dropped.
//
// Market-closed gating
// --------------------
//
// When the authoritative session status (`/api/market/status`) reports
// the market as closed or a holiday, the store pauses the per-symbol
// quote timer.  The last valid quote is preserved and freshness is set
// to `"closed"`.  When the market reopens (detected by the next 60 s
// session poll), polling resumes with an **immediate** fetch — the
// store does not wait for the next 2 s tick.
//
// Tab visibility
// --------------
//
// When the browser tab is hidden the store pauses all quote timers to
// avoid unnecessary API traffic.  When the tab regains visibility (or
// the browser fires an `online` event after a network outage), an
// immediate fetch is triggered for every active subscriber and the
// 2-second timer resumes.
//
// IMPORTANT: polling frequency (0.5 Hz / 2 s) is NOT the same as market
// tick frequency.  A successful REST poll does not mean the exchange
// produced a new tick; freshness is derived from the authoritative
// `marketTimestamp` (exchange tick time), never from the browser clock
// or the request time alone.
//
// Freshness
// ---------
//
// Each entry tracks a `freshness` of one of:
//
// * `"fresh"`   — fetched within the last 5 seconds with a valid price
// * `"stale"`   — last successful fetch > 5 s ago (timer may be
//                 temporarily down, e.g. network blip)
// * `"error"`   — most recent attempt failed; the previous valid quote
//                 (if any) is preserved so the UI does not flash empty
// * `"closed"`  — server reports the market is closed; no ticks expected
//                 until the next session open
// * `"never"`   — no successful fetch yet; only the loading state shows
//
// The store never fabricates prices. If a fetch returns a body without
// `price`, the prior value is preserved and `freshness` becomes
// `"error"` with a message.
//
// Transport-agnostic plug-in point
// --------------------------------
//
// The store only depends on the `MarketDataSource` interface
// (`getQuote` and `getMarketStatus`), so when the WebSocket
// infrastructure replaces the HTTP poll, the store can be re-pointed at
// a new `MarketDataSource` implementation without changing any
// component.

import { dataSource } from "./MarketDataSource";
import type { MarketQuote, LiveMarketState } from "@/types";

export type Freshness = "fresh" | "stale" | "error" | "closed" | "never";

export interface QuoteState {
  data: MarketQuote | null;
  freshness: Freshness;
  /** Epoch ms of the most recent successful fetch. */
  lastSuccessTs: number | null;
  /** Epoch ms of the most recent fetch attempt (success or failure). */
  lastAttemptTs: number | null;
  /** Human-readable error message when freshness === "error". */
  error: string | null;
  /** Epoch ms of the exchange trade tick (authoritative freshness source). */
  marketTimestamp: number | null;
  /** Epoch ms when our system fetched the data (server wall-clock fallback). */
  fetchedAt: number | null;
}

export type { LiveMarketState };

export interface SessionState {
  market: string;
  phase: "pre_market" | "regular" | "post_market" | "closed" | "holiday";
  serverTime: number;
  nextOpen: number | null;
  nextClose: number | null;
}

/**
 * Live quote poll interval: 0.5 Hz = one HTTP request per active symbol
 * every 2 seconds.  Override at build time via VITE_QUOTE_POLL_INTERVAL_MS
 * (follows the existing VITE_* env convention).  Must NOT be confused with
 * OHLCV/history polling, which has a separate lifecycle (see Dashboard).
 */
const _envInterval = Number(import.meta.env.VITE_QUOTE_POLL_INTERVAL_MS);
export const LIVE_QUOTE_POLL_INTERVAL_MS =
  Number.isFinite(_envInterval) && _envInterval > 0 ? _envInterval : 2_000;
export const DEFAULT_POLL_INTERVAL_MS = LIVE_QUOTE_POLL_INTERVAL_MS;
export const DEFAULT_STALE_AFTER_MS = 5_000;

type Listener = (state: QuoteState) => void;
type SessionListener = (state: SessionState | null) => void;
type LiveListener = (state: LiveMarketState | null) => void;

interface SymbolEntry {
  state: QuoteState;
  listeners: Set<Listener>;
  inflight: Promise<void> | null;
  timer: ReturnType<typeof setInterval> | null;
  /** True if at least one listener has requested polling to start. */
  active: boolean;
  /** True when the quote timer is temporarily paused (market closed or tab hidden). */
  paused: boolean;
}

type LiveCacheEntry = {
  state: QuoteState;
  session: SessionState | null;
  value: LiveMarketState;
};

function emptyState(): QuoteState {
  return {
    data: null,
    freshness: "never",
    lastSuccessTs: null,
    lastAttemptTs: null,
    error: null,
    marketTimestamp: null,
    fetchedAt: null,
  };
}

class MarketDataStore {
  private entries = new Map<string, SymbolEntry>();
  private sessionState: SessionState | null = null;
  private sessionListeners = new Set<SessionListener>();
  private sessionTimer: ReturnType<typeof setInterval> | null = null;
  private sessionInflight: Promise<void> | null = null;
  private pollIntervalMs: number = DEFAULT_POLL_INTERVAL_MS;
  private staleAfterMs: number = DEFAULT_STALE_AFTER_MS;
  private clockTimer: ReturnType<typeof setInterval> | null = null;
  private clockListeners = new Set<() => void>();
  private liveCache = new Map<string, LiveCacheEntry>();
  private stopped = false;
  private visibilityListener: (() => void) | null = null;
  private onlineListener: (() => void) | null = null;

  configure(opts: { pollIntervalMs?: number; staleAfterMs?: number }): void {
    if (typeof opts.pollIntervalMs === "number" && opts.pollIntervalMs > 0) {
      this.pollIntervalMs = opts.pollIntervalMs;
      // Restart any active timers with the new interval
      for (const [sym, entry] of this.entries) {
        if (entry.active) {
          this.restartTimer(sym, entry);
        }
      }
    }
    if (typeof opts.staleAfterMs === "number" && opts.staleAfterMs > 0) {
      this.staleAfterMs = opts.staleAfterMs;
    }
  }

  /**
   * Read the current snapshot for ``symbol`` synchronously.  Returns
   * the default (empty) state if no fetch has been triggered yet.
   */
   getState(symbol: string): QuoteState {
     let entry = this.entries.get(symbol);
     if (!entry) {
       entry = {
         state: emptyState(), listeners: new Set(), inflight: null,
         timer: null, active: false, paused: false,
       };
       this.entries.set(symbol, entry);
     }
     return this.computeFreshness(entry);
   }

  /**
   * Subscribe to a symbol.  The first subscriber triggers polling;
   * the last ``unsubscribe`` stops it.  Returns an unsubscribe
   * function.
   */
   subscribe(symbol: string, listener: Listener): () => void {
     this.ensureGlobalListeners();
     let entry = this.entries.get(symbol);
     if (!entry) {
       entry = {
         state: emptyState(),
         listeners: new Set(),
         inflight: null,
         timer: null,
         active: false,
         paused: false,
       };
       this.entries.set(symbol, entry);
     }
     entry.listeners.add(listener);
     if (!entry.active) {
       entry.active = true;
       // Fire an immediate fetch so the UI does not see "loading" while
       // the first 2 s tick is pending.
       void this.fetchOnce(symbol, entry);
       // Only start the recurring timer when polling is not paused
       // (e.g. market closed or tab hidden).  The timer is resumed by
       // updateTimersForSession() / resumeAllQuotePolling() when the
       // blocking condition clears.
       if (!entry.paused && !this.shouldPause()) {
         this.startTimer(symbol, entry);
       } else {
         entry.paused = true;
       }
     } else {
       // Active already — push current snapshot to the new subscriber.
       listener(this.computeFreshness(entry));
     }
     return () => {
       const e = this.entries.get(symbol);
       if (!e) return;
       e.listeners.delete(listener);
       if (e.listeners.size === 0) {
         e.active = false;
         e.paused = false;
         this.clearTimer(e);
         // Keep the last state cached for re-mounts within the same
         // session, but mark the entry as inactive so we don't poll.
       }
     };
   }

  /**
   * Subscribe to the market session status.  The store polls
   * ``/api/market/status`` once on first subscription, then every
   * 60 s.  Returns an unsubscribe function.
   */
   subscribeSession(listener: SessionListener): () => void {
     this.ensureGlobalListeners();
     this.sessionListeners.add(listener);
    if (this.sessionState !== null) listener(this.sessionState);
    if (this.sessionTimer === null) {
      void this.fetchSessionOnce();
      this.sessionTimer = setInterval(() => {
        void this.fetchSessionOnce();
      }, 60_000);
    }
    return () => {
      this.sessionListeners.delete(listener);
      if (this.sessionListeners.size === 0 && this.sessionTimer) {
        clearInterval(this.sessionTimer);
        this.sessionTimer = null;
      }
    };
  }

  getSession(): SessionState | null {
    return this.sessionState;
  }

  /**
   * Subscribe to the canonical LiveMarketState for `symbol`.
   * Pushes a new LiveMarketState whenever the quote or session
   * changes.  Returns an unsubscribe function.
   */
  subscribeLive(symbol: string, listener: LiveListener): () => void {
    const unsub = this.subscribe(symbol, () => {
      listener(this.getLiveMarketState(symbol));
    });
    // Also re-emit on session changes.
    const unsubSession = this.subscribeSession(() => {
      listener(this.getLiveMarketState(symbol));
    });
    listener(this.getLiveMarketState(symbol));
    return () => {
      unsub();
      unsubSession();
    };
  }

   /**
    * Mark the current time so any ``"fresh"`` entry whose last
    * successful fetch is older than ``staleAfterMs`` is demoted to
    * ``"stale"``.  Components can subscribe to clock ticks (1 s) to
    * re-render their freshness badge without polling the server.
    */
  subscribeClock(listener: () => void): () => void {
    this.clockListeners.add(listener);
    if (this.clockTimer === null) {
      this.clockTimer = setInterval(() => {
        // Demote any entries that have crossed the staleness threshold
        for (const [, entry] of this.entries) {
          if (entry.state.freshness === "fresh") {
            const updated = this.computeFreshness(entry);
            if (updated.freshness !== "fresh") {
              entry.state = updated;
              for (const l of entry.listeners) l(updated);
            }
          }
        }
        for (const l of this.clockListeners) l();
      }, 1_000);
    }
    return () => {
      this.clockListeners.delete(listener);
      if (this.clockListeners.size === 0 && this.clockTimer) {
        clearInterval(this.clockTimer);
        this.clockTimer = null;
      }
    };
  }

  /**
   * Tear down every timer and drop all inflight work.  Intended for
   * test teardown and tab-unload safety; safe to call multiple times.
   */
   stop(): void {
     this.stopped = true;
     this.removeGlobalListeners();
     for (const [, entry] of this.entries) {
       this.clearTimer(entry);
       entry.active = false;
       entry.paused = false;
       entry.listeners.clear();
     }
     if (this.sessionTimer) {
       clearInterval(this.sessionTimer);
       this.sessionTimer = null;
     }
     if (this.clockTimer) {
       clearInterval(this.clockTimer);
       this.clockTimer = null;
     }
     this.sessionListeners.clear();
     this.clockListeners.clear();
   }

  /** Test-only reset.  Clears all cached state without disabling the store. */
   __resetForTests(): void {
     this.removeGlobalListeners();
     for (const [, entry] of this.entries) {
       this.clearTimer(entry);
       entry.listeners.clear();
       entry.inflight = null;
       entry.state = emptyState();
       entry.active = false;
       entry.paused = false;
     }
     this.entries.clear();
     this.liveCache.clear();
     if (this.sessionTimer) clearInterval(this.sessionTimer);
     this.sessionTimer = null;
     if (this.clockTimer) clearInterval(this.clockTimer);
     this.clockTimer = null;
     this.sessionState = null;
     this.sessionListeners.clear();
     this.clockListeners.clear();
     this.stopped = false;
   }

  // --- internals -----------------------------------------------------------

   private startTimer(symbol: string, entry: SymbolEntry): void {
     if (entry.timer) return;
     if (entry.paused) return;
     entry.timer = setInterval(() => {
       void this.fetchOnce(symbol, entry);
     }, this.pollIntervalMs);
     // Ensure the clock tick is running so freshness demotion can
     // happen even when no component has explicitly subscribed to it.
     if (this.clockTimer === null) {
       this.clockTimer = setInterval(() => {
         for (const [, e] of this.entries) {
           if (e.state.data === null) continue;
           const updated = this.computeFreshness(e);
           if (updated !== e.state) {
             e.state = updated;
             for (const l of e.listeners) l(updated);
           }
         }
         for (const l of this.clockListeners) l();
       }, 1_000);
     }
   }

   private restartTimer(symbol: string, entry: SymbolEntry): void {
     this.clearTimer(entry);
     if (entry.active && !entry.paused) this.startTimer(symbol, entry);
   }

   private clearTimer(entry: SymbolEntry): void {
     if (entry.timer) {
       clearInterval(entry.timer);
       entry.timer = null;
     }
   }

   // --- global listeners (tab visibility + network recovery) --------------------

   private ensureGlobalListeners(): void {
     if (this.visibilityListener) return;
     if (typeof document === "undefined" || typeof window === "undefined") return;
     this.visibilityListener = () => {
       if (document.hidden) {
         this.pauseAllQuotePolling();
       } else {
         // Tab regained visibility — immediate refresh + resume timer.
         this.resumeAllQuotePolling(true);
       }
     };
     document.addEventListener("visibilitychange", this.visibilityListener);
     this.onlineListener = () => {
       // Network came back — trigger an immediate fetch for every
       // active subscriber rather than waiting up to 2 s for the next
       // scheduled tick.
       this.refreshNow();
     };
     window.addEventListener("online", this.onlineListener);
     // If the tab is already hidden when the store activates, pause now.
     if (document.hidden) {
       this.pauseAllQuotePolling();
     }
   }

   private removeGlobalListeners(): void {
     if (this.visibilityListener) {
       if (typeof document !== "undefined") {
         document.removeEventListener("visibilitychange", this.visibilityListener);
       }
       this.visibilityListener = null;
     }
     if (this.onlineListener) {
       if (typeof window !== "undefined") {
         window.removeEventListener("online", this.onlineListener);
       }
       this.onlineListener = null;
     }
   }

   /**
    * Whether quote polling should be paused right now — i.e. the browser
    * tab is hidden or the market session is known to be closed.
    */
   private shouldPause(): boolean {
     if (this.isTabHidden()) return true;
     const s = this.sessionState;
     if (s && (s.phase === "closed" || s.phase === "holiday")) return true;
     return false;
   }

   private isTabHidden(): boolean {
     return typeof document !== "undefined" && !!document.hidden;
   }

   /** Pause all active quote timers (called when tab hides or market closes). */
   private pauseAllQuotePolling(): void {
     for (const [, entry] of this.entries) {
       if (entry.active && !entry.paused) {
         entry.paused = true;
         this.clearTimer(entry);
       }
     }
   }

   /**
    * Resume all paused quote timers.  When ``immediate`` is true, also
    * fire an immediate fetch for each resumed symbol (used on tab-show
    * and market-open transitions so the UI doesn't wait up to 2 s).
    */
   private resumeAllQuotePolling(immediate: boolean): void {
     if (this.shouldPause()) return;
     for (const [sym, entry] of this.entries) {
       if (entry.active && entry.paused) {
         entry.paused = false;
         if (immediate) void this.fetchOnce(sym, entry);
         this.startTimer(sym, entry);
       }
     }
   }

   /** Resume timers based on the latest session state (market-open transition). */
   private updateTimersForSession(): void {
     const closed = this.sessionState?.phase === "closed" || this.sessionState?.phase === "holiday";
     for (const [sym, entry] of this.entries) {
       if (!entry.active) continue;
       if (closed && !entry.paused) {
         entry.paused = true;
         this.clearTimer(entry);
       } else if (!closed && entry.paused && !this.isTabHidden()) {
         entry.paused = false;
         void this.fetchOnce(sym, entry); // immediate refresh on market-open
         this.startTimer(sym, entry);
       }
     }
   }

   /**
    * Trigger an immediate quote fetch for every active, non-paused symbol.
    * Used for reconnect/network-recovery so the UI doesn't wait for the
    * next scheduled 2 s tick.  Safe to call even when no fetch is needed.
    */
   refreshNow(): void {
     if (typeof document !== "undefined" && document.hidden) return;
     if (this.shouldPause()) return;
      for (const [sym, entry] of this.entries) {
        if (entry.active && !entry.paused) {
          void this.fetchOnce(sym, entry);
        }
      }
    }

  /**
   * Issue exactly one fetch.  Overlap prevention: if a previous
   * fetch for the same symbol is still in flight, we attach to it
   * rather than issuing a parallel request.
   */
  private async fetchOnce(symbol: string, entry: SymbolEntry): Promise<void> {
    if (this.stopped) return;
    if (entry.inflight) {
      return entry.inflight;
    }
    const p = (async () => {
      try {
        const quote = await dataSource.getQuote(symbol);
        if (this.stopped) return;
        // Market-closed state from the wire wins over generic freshness
        const session = this.sessionState;
        const isClosed = session
          ? session.phase === "closed" || session.phase === "holiday"
          : quote.sessionState === "CLOSED";
        const fetchedAt = quote.fetchedAt ?? Date.now();
        const marketTimestamp = quote.marketTimestamp ?? fetchedAt;
        entry.state = {
          data: quote,
          freshness: isClosed ? "closed" : "fresh",
          lastSuccessTs: fetchedAt,
          lastAttemptTs: fetchedAt,
          error: null,
          marketTimestamp,
          fetchedAt,
        };
        for (const l of entry.listeners) l(entry.state);
      } catch (err) {
        if (this.stopped) return;
        const message = err instanceof Error ? err.message : "Unknown error";
        const now = Date.now();
        const had = entry.state.data !== null;
        entry.state = {
          data: entry.state.data,
          freshness: "error",
          lastSuccessTs: entry.state.lastSuccessTs,
          lastAttemptTs: now,
          error: had ? `${message} (showing last known value)` : message,
          marketTimestamp: entry.state.marketTimestamp,
          fetchedAt: entry.state.fetchedAt,
        };
        for (const l of entry.listeners) l(entry.state);
      } finally {
        entry.inflight = null;
      }
    })();
    entry.inflight = p;
    return p;
  }

   private async fetchSessionOnce(): Promise<void> {
     if (this.stopped) return;
     if (this.sessionInflight) return this.sessionInflight;
     this.sessionInflight = (async () => {
       try {
         const s = await dataSource.getMarketStatus();
         if (this.stopped) return;
         this.sessionState = s;
         // Re-evaluate freshness for every active symbol now that we
         // know whether the market is open.
         const isClosed = s.phase === "closed" || s.phase === "holiday";
         for (const [, entry] of this.entries) {
           if (entry.state.data) {
             if (isClosed && entry.state.freshness === "fresh") {
               entry.state = { ...entry.state, freshness: "closed" };
               for (const l of entry.listeners) l(entry.state);
             } else if (!isClosed && entry.state.freshness === "closed") {
               entry.state = {
                 ...entry.state,
                 freshness: "fresh",
                 lastSuccessTs: this.sessionState?.serverTime ?? Date.now(),
               };
               for (const l of entry.listeners) l(entry.state);
             }
           }
         }
         // Pause or resume per-symbol quote timers based on the new
         // session state (market-closed gating + reconnect recovery).
         this.updateTimersForSession();
         for (const l of this.sessionListeners) l(s);
       } catch {
         // Session info is best-effort; keep last good value.
       } finally {
         this.sessionInflight = null;
       }
     })();
     return this.sessionInflight;
   }

  private computeFreshness(entry: SymbolEntry): QuoteState {
    if (entry.state.data === null) return entry.state;
    if (entry.state.freshness === "error") return entry.state;
    if (entry.state.freshness === "closed") return entry.state;
    // Freshness is computed from the AUTHORITATIVE market timestamp
    // (exchange tick time).  If that is unavailable we fall back to the
    // fetch time — but we never claim a quote is fresh based on a
    // client-side clock we don't control.
    const ref = entry.state.marketTimestamp ?? entry.state.lastSuccessTs;
    const age = ref ? Date.now() - ref : Infinity;
    if (age > this.staleAfterMs) {
      // CRITICAL: only allocate a new object when freshness actually
      // transitions.  Returning a fresh object on every call would
      // make useSyncExternalStore loop forever because each render
      // produces a new identity.
      if (entry.state.freshness === "stale") return entry.state;
      entry.state = { ...entry.state, freshness: "stale" };
      return entry.state;
    }
    if (entry.state.freshness === "stale") {
      entry.state = { ...entry.state, freshness: "fresh" };
      return entry.state;
    }
    return entry.state;
  }

  /**
   * Produce the canonical LiveMarketState for ``symbol``.
   *
   * This is the single authoritative representation of the current live
   * market state.  All consumers (Dashboard, Signals, AI Intelligence,
   * Sidebar) should read from this method rather than maintaining their
   * own quote caches.
   */
  getLiveMarketState(symbol: string): LiveMarketState | null {
    const entry = this.entries.get(symbol);
    if (!entry || !entry.state.data) return null;
    const state = this.computeFreshness(entry);
    if (!state.data) return null;
    const cached = this.liveCache.get(symbol);
    if (cached && cached.state === state && cached.session === this.sessionState) {
      return cached.value;
    }
    const now = Date.now();
    const ref = state.marketTimestamp ?? state.lastSuccessTs ?? now;
    const freshnessMs = Math.max(0, now - ref);
    const session = this.sessionState;
    const marketStatus = !session
      ? "UNKNOWN"
      : session.phase === "regular"
        ? "OPEN"
        : session.phase === "pre_market"
          ? "PRE_OPEN"
          : session.phase === "post_market"
            ? "POST_MARKET"
            : session.phase === "closed" || session.phase === "holiday"
              ? "CLOSED"
              : "UNKNOWN";
    const value: LiveMarketState = {
      symbol: state.data.symbol,
      price: state.data.price,
      previousClose: state.data.previousClose,
      change: state.data.change,
      changePct: state.data.changePct,
      dayOpen: state.data.dayOpen,
      dayHigh: state.data.dayHigh,
      dayLow: state.data.dayLow,
      volume: state.data.volume,
      vwap: state.data.vwap,
      marketTimestamp: state.marketTimestamp ?? state.lastSuccessTs ?? now,
      fetchedAt: state.fetchedAt ?? state.lastSuccessTs ?? now,
      source: dataSource.mode === "live" ? "upstox" : "mock",
      marketStatus,
      freshnessMs,
      isStale: freshnessMs > this.staleAfterMs || state.freshness === "stale",
      isLive:
        (marketStatus === "OPEN" || marketStatus === "PRE_OPEN" || marketStatus === "POST_MARKET") &&
        freshnessMs <= this.staleAfterMs &&
        state.freshness !== "error" &&
        state.freshness !== "closed",
    };
    this.liveCache.set(symbol, { state, session: this.sessionState, value });
    return value;
  }
}

export const marketDataStore = new MarketDataStore();
