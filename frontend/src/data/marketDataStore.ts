// Centralized live-quote store.
//
// Architecture overview
// --------------------
//
// Components that need a live quote (Dashboard, Sidebar, TickerStrip,
// AIAnalysis panel, etc.) all consume this store. The store polls
// `GET /api/market/quote` on a single 1 Hz timer **per active symbol**,
// deduping requests across components so three mounts of the dashboard
// trigger exactly one HTTP call per second for the active symbol.
//
// Lifecycle
// ---------
//
// * On the first `subscribe(symbol, listener)` for a given symbol, the
//   store immediately fetches a quote and starts a 1-second timer.
// * On the last `unsubscribe(symbol)` for that symbol, the timer is
//   cleared.
// * On `stop()` (called from the test teardown and on tab unload), all
//   timers are cleared and pending fetches are dropped.
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
import type { MarketQuote } from "@/types";

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
}

export interface SessionState {
  market: string;
  phase: "pre_market" | "regular" | "post_market" | "closed" | "holiday";
  serverTime: number;
  nextOpen: number | null;
  nextClose: number | null;
}

export const DEFAULT_POLL_INTERVAL_MS = 1_000;
export const DEFAULT_STALE_AFTER_MS = 5_000;

type Listener = (state: QuoteState) => void;
type SessionListener = (state: SessionState | null) => void;

interface SymbolEntry {
  state: QuoteState;
  listeners: Set<Listener>;
  inflight: Promise<void> | null;
  timer: ReturnType<typeof setInterval> | null;
  /** True if at least one listener has requested polling to start. */
  active: boolean;
}

function emptyState(): QuoteState {
  return {
    data: null,
    freshness: "never",
    lastSuccessTs: null,
    lastAttemptTs: null,
    error: null,
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
  private stopped = false;

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
    const entry = this.entries.get(symbol);
    if (!entry) return emptyState();
    return this.computeFreshness(entry);
  }

  /**
   * Subscribe to a symbol.  The first subscriber triggers polling;
   * the last ``unsubscribe`` stops it.  Returns an unsubscribe
   * function.
   */
  subscribe(symbol: string, listener: Listener): () => void {
    let entry = this.entries.get(symbol);
    if (!entry) {
      entry = {
        state: emptyState(),
        listeners: new Set(),
        inflight: null,
        timer: null,
        active: false,
      };
      this.entries.set(symbol, entry);
    }
    entry.listeners.add(listener);
    if (!entry.active) {
      entry.active = true;
      // Fire an immediate fetch so the UI does not see "loading" while
      // the first 1s tick is pending.
      void this.fetchOnce(symbol, entry);
      this.startTimer(symbol, entry);
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
        if (e.timer) {
          clearInterval(e.timer);
          e.timer = null;
        }
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
   * Mark the current time so any ``"fresh"`` entry whose last
   * successful fetch is older than ``staleAfterMs`` is demoted to
   * ``"stale"``.  Components can subscribe to clock ticks (60 s) to
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
    for (const [, entry] of this.entries) {
      if (entry.timer) {
        clearInterval(entry.timer);
        entry.timer = null;
      }
      entry.active = false;
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
    for (const [, entry] of this.entries) {
      if (entry.timer) clearInterval(entry.timer);
      entry.timer = null;
      entry.listeners.clear();
      entry.inflight = null;
      entry.state = emptyState();
      entry.active = false;
    }
    this.entries.clear();
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
    if (entry.timer) {
      clearInterval(entry.timer);
      entry.timer = null;
    }
    if (entry.active) this.startTimer(symbol, entry);
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
        entry.state = {
          data: quote,
          freshness: isClosed ? "closed" : "fresh",
          lastSuccessTs: Date.now(),
          lastAttemptTs: Date.now(),
          error: null,
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
        for (const [, entry] of this.entries) {
          if (entry.state.data) {
            const isClosed = s.phase === "closed" || s.phase === "holiday";
            if (isClosed && entry.state.freshness === "fresh") {
              entry.state = { ...entry.state, freshness: "closed" };
              for (const l of entry.listeners) l(entry.state);
            } else if (!isClosed && entry.state.freshness === "closed") {
              entry.state = {
                ...entry.state,
                freshness: "fresh",
                lastSuccessTs: Date.now(),
              };
              for (const l of entry.listeners) l(entry.state);
            }
          }
        }
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
    const age = entry.state.lastSuccessTs
      ? Date.now() - entry.state.lastSuccessTs
      : Infinity;
    if (age > this.staleAfterMs) {
      // CRITICAL: only allocate a new object when freshness actually
      // transitions.  Returning a fresh object on every call would
      // make useSyncExternalStore loop forever because each render
      // produces a new identity.
      if (entry.state.freshness === "stale") return entry.state;
      return { ...entry.state, freshness: "stale" };
    }
    if (entry.state.freshness === "stale") {
      return { ...entry.state, freshness: "fresh" };
    }
    return entry.state;
  }
}

export const marketDataStore = new MarketDataStore();
