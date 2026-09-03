// React bindings for the centralized market-data store.
//
// Components call useQuote(symbol) and receive a synchronous snapshot
// of the current state.  When the store emits a new state, the
// component re-renders.  The hook also subscribes to the store's 1 Hz
// clock tick so the freshness badge updates without an extra server
// round trip.

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { marketDataStore } from "./marketDataStore";
import type { QuoteState, SessionState } from "./marketDataStore";

const sessionSubscribe = (cb: () => void) => marketDataStore.subscribeSession(() => cb());
const sessionGetSnapshot = () => marketDataStore.getSession();

/**
 * Subscribe to the live quote for `symbol`.  Returns the current
 * snapshot, automatically re-rendering on store updates and clock
 * ticks (so the freshness badge updates without an extra fetch).
 */
export function useQuote(symbol: string): QuoteState {
  // useSyncExternalStore needs a stable subscribe identity for a given
  // symbol; otherwise it tears down and re-subscribes on every render
  // and warns about getSnapshot caching.  We memoize the subscribe
  // closure on the symbol.
  const subscribe = useMemo(
    () => (cb: () => void) => marketDataStore.subscribe(symbol, () => cb()),
    [symbol],
  );
  const getSnapshot = useMemo(() => () => marketDataStore.getState(symbol), [symbol]);
  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  // Re-render once per second so the freshness badge ("Updated Xs ago")
  // ticks even when no quote has arrived.
  const [, force] = useState(0);
  useEffect(() => marketDataStore.subscribeClock(() => force((n) => n + 1)), []);

  return state;
}

/**
 * Subscribe to the Indian market session status.  Returns the current
 * snapshot (or `null` until the first poll completes).
 */
export function useMarketStatus(): SessionState | null {
  return useSyncExternalStore(sessionSubscribe, sessionGetSnapshot, sessionGetSnapshot);
}
