import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { MarketQuote } from "@/types";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume, fmtTime } from "@/lib/format";
import { Stat } from "@/components/ui";

export function QuoteHeader({ symbol }: { symbol: string }) {
  const [q, setQ] = useState<MarketQuote | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setError(null);
    dataSource
      .getQuote(symbol)
      .then((r) => {
        if (alive) setQ(r);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load quote");
      });
    return () => {
      alive = false;
    };
  }, [symbol]);
  if (!q && error) {
    return (
      <div className="page-head instrument-head">
        <div className="instrument-id">
          <h1 className="page-title">{symbol.replace("NSE:", "")}</h1>
          <div className="subtitle">Data temporarily unavailable</div>
        </div>
      </div>
    );
  }
  if (!q) return null;
  const up = q.change >= 0;
  return (
    <div className="page-head instrument-head">
      <div className="instrument-id">
        <h1 className="page-title">
          {q.symbol.replace("NSE:", "")} <span className="sym">{q.name}</span>
        </h1>
        <div className="subtitle">
          {q.exchange} · {q.instrumentType.toUpperCase()} · <span className="mono">{q.providerSymbol}</span>
        </div>
      </div>
      <div className="instrument-quote">
        <div className="iq-price mono">{up ? "" : "-"}₹{fmtPrice(Math.abs(q.price))}</div>
        <span className={`chg-pill ${up ? "pos" : "neg"}`}>
          {up ? "▲" : "▼"} {fmtSigned(q.change)} ({fmtPct(q.changePct)})
        </span>
        <div className="iq-meta faint">
          Prev Close <span className="mono">₹{fmtPrice(q.previousClose)}</span> · {fmtTime(q.lastUpdate)}
        </div>
      </div>
    </div>
  );
}

/** Compact terminal-style strip of key session metrics (open/high/low/vol/prev/VWAP). */
export function MetricsStrip({ symbol }: { symbol: string }) {
  const [q, setQ] = useState<MarketQuote | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource
      .getQuote(symbol)
      .then((r) => {
        if (alive) setQ(r);
      })
      .catch(() => {
        if (alive) setQ(null);
      });
    return () => {
      alive = false;
    };
  }, [symbol]);
  if (!q) return null;
  const cells: { k: string; v: string; tone?: "pos" | "neg" }[] = [
    { k: "Open", v: `₹${fmtPrice(q.dayOpen)}` },
    { k: "High", v: `₹${fmtPrice(q.dayHigh)}`, tone: "pos" },
    { k: "Low", v: `₹${fmtPrice(q.dayLow)}`, tone: "neg" },
    { k: "Volume", v: fmtVolume(q.volume) },
    { k: "Prev Close", v: `₹${fmtPrice(q.previousClose)}` },
    { k: "VWAP", v: `₹${fmtPrice(q.vwap)}` },
  ];
  return (
    <div className="metric-strip" role="list">
      {cells.map((c) => (
        <div className="ms-cell" role="listitem" key={c.k}>
          <span className="ms-k">{c.k}</span>
          <span className={`ms-v mono${c.tone ? " " + c.tone : ""}`}>{c.v}</span>
        </div>
      ))}
    </div>
  );
}

export function MetricsPanel({ symbol }: { symbol: string }) {
  const [q, setQ] = useState<MarketQuote | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource
      .getQuote(symbol)
      .then((r) => {
        if (alive) setQ(r);
      })
      .catch(() => {
        if (alive) setQ(null);
      });
    return () => {
      alive = false;
    };
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
