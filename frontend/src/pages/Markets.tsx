import { useMemo, useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { INSTRUMENTS, mockQuote, mockAIAnalysis, WATCHLIST_SYMBOLS, mockOHLCV } from "@/data/mock";
import { useApp } from "@/store/AppContext";
import { Badge, EmptyState } from "@/components/ui";
import { Sparkline } from "@/components/charts/Sparkline";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume } from "@/lib/format";
import type { OHLCVBar } from "@/types";

type Row = ReturnType<typeof mockQuote> & { trend: string; signal: string; spark: number[] };

function buildRows(): Row[] {
  return INSTRUMENTS.map((m) => {
    const q = mockQuote(m.symbol);
    const ai = mockAIAnalysis(m.symbol);
    const bars: OHLCVBar[] = mockOHLCV(m.symbol, "1D", 30);
    return { ...q, trend: ai.bias, signal: ai.signal, spark: bars.map((b) => b.close) };
  });
}

export function MarketsPage() {
  const navigate = useNavigate();
  const { setSelectedSymbol } = useApp();
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | "index" | "equity">("all");
  const [rows, setRows] = useState<Row[]>([]);

  useEffect(() => { setRows(buildRows()); }, []);

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        const matchQ = `${r.symbol} ${r.name}`.toLowerCase().includes(q.toLowerCase());
        const matchT = typeFilter === "all" ? true : r.instrumentType === typeFilter;
        return matchQ && matchT;
      }),
    [rows, q, typeFilter],
  );

  const adv = filtered.filter((r) => r.change >= 0).length;
  const dec = filtered.length - adv;

  const open = (sym: string) => {
    setSelectedSymbol(sym);
    navigate("/");
  };

  const Table = ({ title, data }: { title: string; data: Row[] }) => (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <span className="panel-title">{title}</span>
        <span className="faint" style={{ fontSize: 11 }}>{data.length} symbols</span>
      </div>
      {data.length === 0 ? (
        <EmptyState title="No matches" hint="Try a different search term." />
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Price</th>
              <th className="num">Change</th>
              <th className="num">Chg %</th>
              <th className="num">Volume</th>
              <th className="spark-col">Trend</th>
              <th>Trend</th>
              <th>Signal</th>
            </tr>
          </thead>
          <tbody>
            {data.map((r) => {
              const up = r.change >= 0;
              return (
                <tr key={r.symbol} onClick={() => open(r.symbol)} tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && open(r.symbol)}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{r.symbol.replace("NSE:", "")}</div>
                    <div className="faint" style={{ fontSize: 11 }}>{r.name}</div>
                  </td>
                  <td className="num mono">₹{fmtPrice(r.price)}</td>
                  <td className={`num mono ${up ? "pos" : "neg"}`}>{fmtSigned(r.change)}</td>
                  <td className={`num mono ${up ? "pos" : "neg"}`}>{fmtPct(r.changePct)}</td>
                  <td className="num mono">{fmtVolume(r.volume)}</td>
                  <td className="spark-col">
                    <Sparkline data={r.spark} positive={up} width={84} height={24} />
                  </td>
                  <td><Badge kind={r.trend}>{r.trend}</Badge></td>
                  <td><Badge kind={r.signal}>{r.signal.replace("_", " ")}</Badge></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Markets</h1>
          <div className="subtitle">Indices & equities — click a row to open its dashboard.</div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <div className="search">
            <span className="faint">⌕</span>
            <input
              placeholder="Search symbol or name…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              aria-label="Search markets"
            />
          </div>
          <div className="tf-controls" role="group" aria-label="Filter by type">
            {(["all", "index", "equity"] as const).map((t) => (
              <button key={t} className={typeFilter === t ? "active" : ""} onClick={() => setTypeFilter(t)} aria-pressed={typeFilter === t}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid cols-3" style={{ gap: 12 }}>
        <div className="stat-card">
          <span className="label">Instruments</span>
          <span className="value">{filtered.length}</span>
        </div>
        <div className="stat-card">
          <span className="label">Advancing</span>
          <span className="value pos">{adv}</span>
        </div>
        <div className="stat-card">
          <span className="label">Declining</span>
          <span className="value neg">{dec}</span>
        </div>
      </div>

      <Table title="Indices" data={filtered.filter((r) => r.instrumentType === "index")} />
      <Table title="Equities" data={filtered.filter((r) => r.instrumentType === "equity")} />
      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>Mock market data.</p>
    </>
  );
}

void WATCHLIST_SYMBOLS;
