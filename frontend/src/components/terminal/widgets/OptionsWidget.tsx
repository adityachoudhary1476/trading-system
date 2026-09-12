"use client";

import { useQuery } from "@tanstack/react-query";
import { useState, useMemo } from "react";
import { apiGet, fmt, fmtBig } from "@/lib/api";
import { useWidgetSymbol, type WidgetInstance } from "@/store/terminal";

type OptionRow = {
  strike: number | null;
  lastPrice: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  openInterest: number | null;
  impliedVolatility?: number | null;
  inTheMoney: boolean;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
};

type Chain = {
  symbol: string;
  underlyingPrice: number | null;
  expirationDates: string[];
  selectedDate: string | null;
  calls: OptionRow[];
  puts: OptionRow[];
};

export function OptionsWidget({ _widget }: { _widget: WidgetInstance }) {
  const symbol = useWidgetSymbol(_widget);
  const [expiry, setExpiry] = useState<string | undefined>();

  const { data, error, isLoading } = useQuery({
    queryKey: ["options", symbol, expiry],
    queryFn: () => apiGet<Chain>(`/api/market/options/${symbol}${expiry ? `?expiry=${encodeURIComponent(expiry)}` : ""}`),
    refetchInterval: 20_000,
    retry: 0,
    staleTime: 10_000,
  });

  if (error)
    return (
      <div className="p-2">
        <div className="down">Option chain unavailable for {symbol}</div>
        <div className="dim">{(error as Error).message}</div>
      </div>
    );
  if (isLoading || !data) return <div className="p-2 dim">Loading option chain…</div>;

  const byStrike = new Map<number, { call?: OptionRow; put?: OptionRow }>();
  for (const c of data.calls) if (c.strike !== null) byStrike.set(c.strike, { ...byStrike.get(c.strike), call: c });
  for (const p of data.puts) if (p.strike !== null) byStrike.set(p.strike, { ...byStrike.get(p.strike), put: p });
  const strikes = [...byStrike.keys()].sort((a, b) => a - b);

  const atmStrike = useMemo(() => {
    if (!data.underlyingPrice) return strikes[Math.floor(strikes.length / 2)];
    return strikes.reduce((prev, curr) => Math.abs(curr - data.underlyingPrice!) < Math.abs(prev - data.underlyingPrice!) ? curr : prev);
  }, [data.underlyingPrice, strikes]);

  return (
    <div className="h-full flex flex-col">
      <div className="flex gap-2 items-center p-1 shrink-0 border-b border-[var(--border)]">
        <span className="dim">Underlying</span>
        <span className="amber font-bold">{fmt(data.underlyingPrice)}</span>
        <span className="dim ml-2">Expiry</span>
        <select
          value={expiry ?? data.selectedDate ?? ""}
          onChange={(e) => setExpiry(e.target.value || undefined)}
          className="bg-[var(--bg-elev-2)] border border-[var(--border)] rounded px-2 py-1 text-[var(--text)] text-[11px]"
        >
          {data.expirationDates.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
        <span className="ml-auto dim text-[10px]">
          ATM: <span className="amber font-bold">{fmt(atmStrike)}</span>
        </span>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="data-table w-full" style={{ fontSize: 11 }}>
          <thead className="sticky top-0 bg-[var(--bg-elev)]">
            <tr className="border-b border-[var(--border)]">
              <th colSpan={6} className="!text-center up p-1">CALLS</th>
              <th className="!text-center p-1 amber font-bold sticky left-0 z-10 bg-[var(--bg-elev)]">STRIKE</th>
              <th colSpan={6} className="!text-center down p-1">PUTS</th>
            </tr>
            <tr className="border-b border-[var(--border-soft)] text-[9px] text-[var(--text-faint)]">
              <th className="p-1">LTP</th><th className="p-1">Bid</th><th className="p-1">Ask</th><th className="p-1">Vol</th><th className="p-1">OI</th><th className="p-1">IV</th>
              <th className="p-1"></th>
              <th className="p-1">LTP</th><th className="p-1">Bid</th><th className="p-1">Ask</th><th className="p-1">Vol</th><th className="p-1">OI</th><th className="p-1">IV</th>
            </tr>
          </thead>
          <tbody>
            {strikes.map((strike) => {
              const entry = byStrike.get(strike);
              const call = entry?.call;
              const put = entry?.put;
              const isATM = strike === atmStrike;
              return (
                <tr key={strike} className={isATM ? "bg-[rgba(255,153,0,0.08)]" : ""}>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.lastPrice !== null && call?.lastPrice !== undefined ? fmt(call.lastPrice) : "—"}
                  </td>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.bid !== null && call?.bid !== undefined ? fmt(call.bid) : "—"}
                  </td>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.ask !== null && call?.ask !== undefined ? fmt(call.ask) : "—"}
                  </td>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.volume !== null && call?.volume !== undefined ? fmtBig(call.volume) : "—"}
                  </td>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.openInterest !== null && call?.openInterest !== undefined ? fmtBig(call.openInterest) : "—"}
                  </td>
                  <td className={call?.inTheMoney ? "bg-[#0d2010] p-1" : "p-1"}>
                    {call?.impliedVolatility !== null && call?.impliedVolatility !== undefined
                      ? fmt(call.impliedVolatility * 100, 1) + "%"
                      : "—"}
                  </td>
                  <td className="!text-center font-bold amber p-1 sticky left-0 z-10 bg-[var(--bg-elev)] border-r border-[var(--border)]">
                    {fmt(strike)}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.lastPrice !== null && put?.lastPrice !== undefined ? fmt(put.lastPrice) : "—"}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.bid !== null && put?.bid !== undefined ? fmt(put.bid) : "—"}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.ask !== null && put?.ask !== undefined ? fmt(put.ask) : "—"}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.volume !== null && put?.volume !== undefined ? fmtBig(put.volume) : "—"}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.openInterest !== null && put?.openInterest !== undefined ? fmtBig(put.openInterest) : "—"}
                  </td>
                  <td className={put?.inTheMoney ? "bg-[#200d0d] p-1" : "p-1"}>
                    {put?.impliedVolatility !== null && put?.impliedVolatility !== undefined
                      ? fmt(put.impliedVolatility * 100, 1) + "%"
                      : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}