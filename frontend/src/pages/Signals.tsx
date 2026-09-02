import { useMemo, useState, useEffect } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { Signal, SignalDirection } from "@/types";
import { Badge, EmptyState } from "@/components/ui";
import { fmtPrice, fmtDateTime } from "@/lib/format";

const SIGNAL_FILTERS: ("all" | SignalDirection)[] = ["all", "long", "short", "hold", "no_signal"];

export function SignalsPage() {
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sym, setSym] = useState("");
  const [dir, setDir] = useState<"all" | SignalDirection>("all");

  useEffect(() => {
    let alive = true;
    setError(null);
    dataSource
      .getSignals(18)
      .then((r) => {
        if (alive) setSignals(r);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load signals");
      });
    return () => {
      alive = false;
    };
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

  const counts = useMemo(() => {
    const c: Record<string, number> = { long: 0, short: 0, hold: 0, no_signal: 0 };
    signals?.forEach((s) => { c[s.direction] = (c[s.direction] ?? 0) + 1; });
    return c;
  }, [signals]);

  if (error) {
    return (
      <>
        <div className="page-head"><h1 className="page-title">Signals</h1></div>
        <div className="panel">
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">⚠</div>
            <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Unable to load signals</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
          </div>
        </div>
      </>
    );
  }

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
            {symbols.map((s) => <option key={s} value={s}>{(s || "").replace("NSE:", "")}</option>)}
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

      <div className="grid cols-4" style={{ gap: 12, marginBottom: 16 }}>
        {(["long", "short", "hold", "no_signal"] as const).map((d) => (
          <div className="stat-card" key={d}>
            <span className="label">{(d === "no_signal" ? "No Signal" : d[0].toUpperCase() + d.slice(1))}</span>
            <span className={`value`}>{counts[d] ?? 0}</span>
          </div>
        ))}
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
              {rows.map((s) => {
                const conf = Math.round((s.confidence ?? 0) * 100);
                const direction = s.direction ?? "no_signal";
                const symbol = s.symbol ?? "";
                return (
                  <tr key={s.id} className={`sig-row sig-${direction}`}>
                    <td className="mono">{fmtDateTime(s.generatedAt)}</td>
                    <td style={{ fontWeight: 600 }}>{symbol.replace("NSE:", "")}</td>
                    <td><Badge kind={direction}>{direction.replace("_", " ")}</Badge></td>
                    <td className="num" style={{ minWidth: 120 }}>
                      <div className="conf-bar" style={{ marginTop: 2 }}>
                        <span className={direction === "short" ? "neg" : direction === "long" ? "pos" : "muted"} style={{ width: `${conf}%` }} />
                      </div>
                      <span className="mono" style={{ fontSize: 11 }}>{conf}%</span>
                    </td>
                    <td className="mono">₹{fmtPrice(s.price ?? 0)}</td>
                    <td><Badge kind={s.bias}>{s.bias}</Badge></td>
                    <td className="muted" style={{ maxWidth: 360 }}>{s.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>Mock signal history.</p>
    </>
  );
}
