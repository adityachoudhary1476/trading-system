import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { INSTRUMENTS, mockQuote, mockAIAnalysis } from "@/data/mock";
import { useApp } from "@/store/AppContext";
import { Badge, EmptyState } from "@/components/ui";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume } from "@/lib/format";

type Row = ReturnType<typeof mockQuote> & { trend: string; signal: string };

function buildRows(): Row[] {
  return INSTRUMENTS.map((m) => {
    const q = mockQuote(m.symbol);
    const ai = mockAIAnalysis(m.symbol);
    return { ...q, trend: ai.bias, signal: ai.signal };
  });
}

export function MarketsPage() {
  const navigate = useNavigate();
  const { setSelectedSymbol } = useApp();
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | "index" | "equity">("all");

  const rows = useMemo(buildRows, []);
  const filtered = rows.filter((r) => {
    const matchQ = `${r.symbol} ${r.name}`.toLowerCase().includes(q.toLowerCase());
    const matchT = typeFilter === "all" ? true : r.instrumentType === typeFilter;
    return matchQ && matchT;
  });

  const indices = filtered.filter((r) => r.instrumentType === "index");
  const equities = filtered.filter((r) => r.instrumentType === "equity");

  const open = (sym: string) => {
    setSelectedSymbol(sym);
    navigate("/");
  };

  const Table = ({ title, data }: { title: string; data: Row[] }) => (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="panel-head"><span className="panel-title">{title}</span></div>
      {data.length === 0 ? (
        <EmptyState title="No matches" hint="Try a different search term." />
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Symbol</th><th>Price</th><th>Change</th><th>Chg %</th><th>Volume</th><th>Trend</th><th>Signal</th>
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
                  <td>₹{fmtPrice(r.price)}</td>
                  <td className={up ? "pos" : "neg"}>{fmtSigned(r.change)}</td>
                  <td className={up ? "pos" : "neg"}>{fmtPct(r.changePct)}</td>
                  <td>{fmtVolume(r.volume)}</td>
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
      <Table title="Indices" data={indices} />
      <Table title="Equities" data={equities} />
      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>Mock market data.</p>
    </>
  );
}
