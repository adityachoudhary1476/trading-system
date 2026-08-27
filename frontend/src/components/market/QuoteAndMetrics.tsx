import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { MarketQuote } from "@/types";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume } from "@/lib/format";
import { Stat } from "@/components/ui";

export function QuoteHeader({ symbol }: { symbol: string }) {
  const [q, setQ] = useState<MarketQuote | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getQuote(symbol).then((r) => alive && setQ(r));
    return () => { alive = false; };
  }, [symbol]);
  if (!q) return null;
  const up = q.change >= 0;
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">
          {q.symbol.replace("NSE:", "")} <span className="sym">{q.name}</span>
        </h1>
        <div className="subtitle">
          {q.exchange} · {q.providerSymbol} · {q.instrumentType.toUpperCase()}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16 }}>
        <div style={{ fontSize: 28, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>
          ₹{fmtPrice(q.price)}
        </div>
        <div style={{ textAlign: "right" }}>
          <div className={up ? "pos" : "neg"} style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
            {fmtSigned(q.change)} ({fmtPct(q.changePct)})
          </div>
          <div className="faint" style={{ fontSize: 11 }}>
            prev close ₹{fmtPrice(q.previousClose)}
          </div>
        </div>
      </div>
    </div>
  );
}

export function MetricsPanel({ symbol }: { symbol: string }) {
  const [q, setQ] = useState<MarketQuote | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getQuote(symbol).then((r) => alive && setQ(r));
    return () => { alive = false; };
  }, [symbol]);
  if (!q) return null;
  const rows: [string, string][] = [
    ["Open", fmtPrice(q.dayOpen)],
    ["High", fmtPrice(q.dayHigh)],
    ["Low", fmtPrice(q.dayLow)],
    ["Prev Close", fmtPrice(q.previousClose)],
    ["Volume", fmtVolume(q.volume)],
    ["VWAP", fmtPrice(q.vwap)],
    ["Day Range", q.dayRange],
    ["Volatility", `${(q.volatility * 100).toFixed(1)}%`],
  ];
  return (
    <div className="grid cols-4" style={{ gap: 10 }}>
      {rows.map(([k, v]) => (
        <Stat key={k} label={k} value={<span className="mono">{v}</span>} />
      ))}
    </div>
  );
}
