"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { apiGet, fmt, fmtBig, type MarketQuote } from "@/lib/api";
import { useTerminal } from "@/store/terminal";
import { Sparkline } from "@/components/charts/Sparkline";

const DEFAULT_WATCHLIST = [
  "NSE:NIFTY50",
  "NSE:NIFTYBANK",
  "NSE:FINNIFTY",
  "NSE:RELIANCE",
  "NSE:TCS",
  "NSE:HDFCBANK",
  "NSE:ICICIBANK",
  "NSE:INFY",
  "NSE:ITC",
  "NSE:SBIN",
];

export function WatchlistWidget({ widget }: { widget: WidgetInstance }) {
  const { selectedSymbol, setSelectedSymbol } = useTerminal();
  const [watchlist, setWatchlist] = useState<string[]>(() => {
    try {
      const stored = localStorage.getItem("finova-watchlist");
      return stored ? JSON.parse(stored) : DEFAULT_WATCHLIST;
    } catch {
      return DEFAULT_WATCHLIST;
    }
  });

  const { data: quotes } = useQuery({
    queryKey: ["watchlist-quotes", watchlist.join(",")],
    queryFn: async () => {
      const results: Record<string, MarketQuote | null> = {};
      await Promise.all(
        watchlist.map(async (sym) => {
          try {
            const q = await apiGet<MarketQuote>(`/api/market/quote?symbol=${encodeURIComponent(sym)}`);
            results[sym] = q;
          } catch {
            results[sym] = null;
          }
        })
      );
      return results;
    },
    refetchInterval: 5_000,
    staleTime: 2_000,
  });

  const { data: sparklines } = useQuery({
    queryKey: ["watchlist-sparklines", watchlist.join(",")],
    queryFn: async () => {
      const results: Record<string, number[]> = {};
      await Promise.all(
        watchlist.map(async (sym) => {
          try {
            const bars = await apiGet<{ time: number; close: number }[]>(
              `/api/market/ohlcv?symbol=${encodeURIComponent(sym)}&timeframe=1D&bars=40`
            );
            results[sym] = bars.map((b) => b.close);
          } catch {
            results[sym] = [];
          }
        })
      );
      return results;
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const addSymbol = async (newSym: string) => {
    const upper = newSym.trim().toUpperCase();
    if (!upper || watchlist.includes(upper)) return;
    try {
      await apiGet(`/api/market/quote?symbol=${encodeURIComponent(upper)}`);
      const next = [...watchlist, upper];
      setWatchlist(next);
      localStorage.setItem("finova-watchlist", JSON.stringify(next));
    } catch {
      alert(`Could not add ${upper} — symbol not found`);
    }
  };

  const removeSymbol = (sym: string) => {
    const next = watchlist.filter((s) => s !== sym);
    setWatchlist(next);
    localStorage.setItem("finova-watchlist", JSON.stringify(next));
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">WATCHLIST</span>
        <span className="flex-1" />
        <button
          onClick={() => {
            const sym = prompt("Add symbol (e.g., NSE:RELIANCE):");
            if (sym) addSymbol(sym);
          }}
          className="term-btn text-[10px] px-2 py-1"
          title="Add symbol"
        >
          +
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {watchlist.map((sym) => {
          const quote = quotes?.[sym] ?? null;
          const series = sparklines?.[sym] ?? [];
          const active = sym === selectedSymbol;
          const up = quote?.changePct !== undefined ? quote.changePct >= 0 : true;
          return (
            <button
              key={sym}
              onClick={() => setSelectedSymbol(sym)}
              className={`w-full text-left px-2 py-1.5 hover:bg-[#1a1a1a] transition-colors border-b border-[var(--border-soft)] ${active ? "bg-[var(--accent-soft)] border-l-2 border-l-[var(--amber)]" : ""}`}
              style={{ borderLeftWidth: active ? 2 : 0 }}
            >
              <div className="flex items-center justify-between gap-2">
                <span className={`font-mono text-[12px] ${active ? "text-[var(--amber)]" : "text-[var(--text)]"}`}>
                  {sym.replace("NSE:", "")}
                </span>
                <span className="dim text-[10px] truncate max-w-[80px]">{sym.replace("NSE:", "")}</span>
              </div>
              <div className="flex items-center justify-between gap-2 mt-1">
                <span className="font-mono text-[12px] tabular-nums">
                  {quote ? `₹${fmt(Math.abs(quote.price))}` : "—"}
                </span>
                <span className={`font-mono text-[10px] tabular-nums ${up ? "up" : "down"}`}>
                  {quote?.changePct !== undefined
                    ? `${quote.changePct >= 0 ? "+" : ""}${quote.changePct.toFixed(2)}%`
                    : "—"}
                </span>
                <span className="flex-1" />
                <button
                  onClick={(e) => { e.stopPropagation(); removeSymbol(sym); }}
                  className="dim hover:text-[var(--down)] text-[10px] p-0.5"
                  title="Remove"
                >
                  ✕
                </button>
              </div>
              <div className="mt-1">
                <Sparkline data={series} positive={up} width="100%" height={18} />
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}