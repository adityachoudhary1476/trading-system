import { useMemo, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { Signal, SignalDirection } from "@/types";
import { useEffect } from "react";
import { Badge, EmptyState } from "@/components/ui";
import { fmtPrice, fmtPct, fmtDateTime } from "@/lib/format";

const SIGNAL_FILTERS: ("all" | SignalDirection)[] = ["all", "long", "short", "hold", "no_signal"];

export function SignalsPage() {
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [sym, setSym] = useState("");
  const [dir, setDir] = useState<"all" | SignalDirection>("all");

  useEffect(() => {
    let alive = true;
    dataSource.getSignals(18).then((r) => alive && setSignals(r));
    return () => { alive = false; };
  }, []);

  const symbols = useMemo(
    () => (signals ? Array.from(new Set(signals.map((s) => s.symbol))) : []),
    [signals],
  );

  const rows = useMemo(() => {
    if (!signals) return [];
    return signals
      .filter((s) => (dir === "all" ? true : s.direction === dir))
      .filter((s) => (sym ? s.symbol === sym : true));
  }, [signals, dir, sym]);

  if (!signals) {
    return (
      <>
        <div className="page-head"><h1 className="page-title">Signals</h1></div>
        <div className="empty"><div className="spinner" /></div>
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Signals</h1>
          <div className="subtitle">Analytical signals only — no order execution.</div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <select className="search" value={sym} onChange={(e) => setSym(e.target.value)} aria-label="Filter by symbol"
            style={{ padding: 7, color: "var(--text)", background: "var(--bg-elev-2)", border: "1px solid var(--panel-border)", borderRadius: 6 }}>
            <option value="">All symbols</option>
            {symbols.map((s) => <option key={s} value={s}>{s.replace("NSE:", "")}</option>)}
          </select>
          <div className="tf-controls" role="group" aria-label="Filter by signal">
            {SIGNAL_FILTERS.map((d) => (
              <button key={d} className={dir === d ? "active" : ""} onClick={() => setDir(d)} aria-pressed={dir === d}>
                {d === "all" ? "All" : d.replace("_", " ")}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="panel">
        {rows.length === 0 ? (
          <EmptyState title="No signals match" hint="Adjust the symbol or signal filter." />
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Time</th><th>Symbol</th><th>Signal</th><th>Confidence</th><th>Price</th><th>Bias</th><th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{fmtDateTime(s.generatedAt)}</td>
                  <td style={{ fontWeight: 600 }}>{s.symbol.replace("NSE:", "")}</td>
                  <td><Badge kind={s.direction}>{s.direction.replace("_", " ")}</Badge></td>
                  <td className="mono">{fmtPct(s.confidence * 100, 0).replace("%", "")}%</td>
                  <td className="mono">₹{fmtPrice(s.price)}</td>
                  <td><Badge kind={s.bias}>{s.bias}</Badge></td>
                  <td className="muted" style={{ maxWidth: 360 }}>{s.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>Mock signal history.</p>
    </>
  );
}
