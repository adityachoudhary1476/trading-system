"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet, fmt, fmtBig, pctClass, type Quote } from "@/lib/api";
import { useWidgetSymbol, type WidgetInstance } from "@/store/terminal";

type ShortVolume = { date: string; shortVolume: number; shortExemptVolume: number; totalVolume: number; shortVolumePercent: number };

export function QuoteWidget({ widget }: { widget: WidgetInstance }) {
  const symbol = useWidgetSymbol(widget);
  const { data, error } = useQuery({
    queryKey: ["quote", symbol],
    queryFn: async () => (await apiGet<Quote[]>(`/api/market/quote?symbol=${encodeURIComponent(symbol)}`))[0],
    refetchInterval: 2_000,
    staleTime: 1_000,
  });

  if (error) return <div className="p-2 down">Error: {(error as Error).message}</div>;
  if (!data) return <div className="p-2 dim">Loading {symbol}…</div>;

  const rows: Array<[string, string, string?]> = [
    ["Open", fmt(data.open)],
    ["High", fmt(data.high)],
    ["Low", fmt(data.low)],
    ["Prev Close", fmt(data.previousClose)],
    ["Bid", fmt(data.bid)],
    ["Ask", fmt(data.ask)],
    ["Bid-Ask Spread", data.bid !== null && data.ask !== null ? fmt(data.ask - data.bid) : "—"],
    ["Volume", fmtBig(data.volume)],
    ["Avg Vol 3M", fmtBig(data.avgVolume)],
    ["Mkt Cap", fmtBig(data.marketCap)],
    ["P/E (ttm)", fmt(data.pe)],
    ["EPS (ttm)", fmt(data.eps)],
    ["Div Yield", data.dividendYield !== null ? fmt(data.dividendYield * 100) + "%" : "—"],
    ["52W High", fmt(data.week52High)],
    ["52W Low", fmt(data.week52Low)],
    ["Beta", fmt(data.beta)],
    ["Shares Out", fmtBig(data.sharesOutstanding)],
  ];

  return (
    <div className="p-2 h-full flex flex-col">
      <div className="flex items-baseline gap-3 mb-2 shrink-0">
        <span className="text-xl font-bold font-mono">{fmt(data.price)}</span>
        <span className={`${pctClass(data.changePercent)} text-sm font-mono`}>
          {data.change !== null && data.change >= 0 ? "+" : ""}
          {fmt(data.change)} ({fmt(data.changePercent)}%)
        </span>
        <span className="dim text-[10px] ml-auto">
          {data.exchange ?? ""} · {data.currency ?? ""} · {data.source}
        </span>
      </div>
      <div className="dim text-[11px] mb-2 truncate">{data.name}</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 flex-1 overflow-auto">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between border-b border-[var(--border-soft)] py-0.5">
            <span className="dim text-[11px]">{label}</span>
            <span className="text-[11px] font-mono text-right">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}