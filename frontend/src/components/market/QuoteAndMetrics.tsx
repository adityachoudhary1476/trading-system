import { useEffect, useState } from "react";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume, fmtTimeIST } from "@/lib/format";
import { Stat } from "@/components/ui";
import { useQuote, useMarketStatus } from "@/data/useQuote";
import type { Freshness } from "@/data/marketDataStore";

function freshnessLabel(f: Freshness, ageSec: number | null): string {
  if (f === "never") return "Loading…";
  if (f === "error") return "Data stale";
  if (f === "closed") return "Market closed";
  if (f === "stale") return "Data stale";
  if (ageSec === null) return "Updated";
  if (ageSec < 2) return "Updated just now";
  return `Updated ${ageSec}s ago`;
}

function freshnessClass(f: Freshness): string {
  if (f === "fresh") return "fresh";
  if (f === "closed") return "closed";
  return "stale";
}

export function QuoteHeader({ symbol }: { symbol: string }) {
  const { data: q, freshness, error, lastSuccessTs } = useQuote(symbol);
  const status = useMarketStatus();
  const [, force] = useState(0);
  // Re-render every second so the "Updated Xs ago" badge ticks
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const ageSec =
    lastSuccessTs && freshness === "fresh"
      ? Math.max(0, Math.floor((Date.now() - lastSuccessTs) / 1000))
      : null;

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
  if (!q) {
    return (
      <div className="page-head instrument-head">
        <div className="instrument-id">
          <h1 className="page-title">{symbol.replace("NSE:", "")}</h1>
          <div className="subtitle">Loading…</div>
        </div>
      </div>
    );
  }
  const up = q.change !== undefined ? q.change >= 0 : true;
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
        <div className={`iq-meta faint freshness ${freshnessClass(freshness)}`}>
          Prev Close <span className="mono">₹{fmtPrice(q.previousClose)}</span>
          {" · "}
          {status && (status.phase === "closed" || status.phase === "holiday")
            ? "Market closed"
            : `As of ${fmtTimeIST(q.lastUpdate)}`}
          {" · "}
          <span data-testid="freshness">{freshnessLabel(freshness, ageSec)}</span>
        </div>
      </div>
    </div>
  );
}

/** Compact terminal-style strip of key session metrics (open/high/low/vol/prev/VWAP). */
export function MetricsStrip({ symbol }: { symbol: string }) {
  const { data: q } = useQuote(symbol);
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
  const { data: q } = useQuote(symbol);
  if (!q) return null;
  const rows: [string, string][] = [
    ["Open", fmtPrice(q.dayOpen)],
    ["High", fmtPrice(q.dayHigh)],
    ["Low", fmtPrice(q.dayLow)],
    ["Prev Close", fmtPrice(q.previousClose)],
    ["Volume", fmtVolume(q.volume)],
    ["VWAP", fmtPrice(q.vwap)],
    ["Day Range", q.dayRange ?? "—"],
    ["Volatility", q.volatility !== undefined ? `${(q.volatility * 100).toFixed(1)}%` : "—"],
  ];
  return (
    <div className="grid cols-4" style={{ gap: 10 }}>
      {rows.map(([k, v]) => (
        <Stat key={k} label={k} value={<span className="mono">{v}</span>} />
      ))}
    </div>
  );
}
