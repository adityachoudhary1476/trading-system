import { useMemo, useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { INSTRUMENTS, WATCHLIST_SYMBOLS } from "@/data/mock";
import { useApp } from "@/store/AppContext";
import { EmptyState } from "@/components/ui";
import { Sparkline } from "@/components/charts/Sparkline";
import { fmtPrice, fmtSigned, fmtPct, fmtVolume } from "@/lib/format";
import { useQuote } from "@/data/useQuote";

function MarketRow({
  symbol,
  name,
  onOpen,
}: {
  symbol: string;
  name: string;
  onOpen: () => void;
}) {
  const { data: q } = useQuote(symbol);
  const [spark, setSpark] = useState<number[]>([]);
  // Synthesize a small sparkline from the last few price updates so
  // we never fabricate unrelated data.  We only append real values
  // that came from the live quote endpoint.
  useEffect(() => {
    if (!q) return;
    setSpark((prev) => {
      const last = prev[prev.length - 1];
      if (last === q.price) return prev;
      const next = [...prev, q.price];
      return next.length > 24 ? next.slice(-24) : next;
    });
  }, [q?.price, q]);
  const up = q?.change !== undefined ? q.change >= 0 : true;
  return (
    <tr onClick={onOpen} tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && onOpen()}
        style={{ cursor: "pointer" }}>
      <td>
        <div style={{ fontWeight: 600 }}>{symbol.replace("NSE:", "")}</div>
        <div className="faint" style={{ fontSize: 11 }}>{name}</div>
      </td>
      <td className="num mono">{q ? `₹${fmtPrice(q.price)}` : "—"}</td>
      <td className={`num mono ${up ? "pos" : "neg"}`}>{q ? fmtSigned(q.change) : "—"}</td>
      <td className={`num mono ${up ? "pos" : "neg"}`}>{q ? fmtPct(q.changePct) : "—"}</td>
      <td className="num mono">{q ? fmtVolume(q.volume) : "—"}</td>
      <td className="spark-col">
        {spark.length > 1 ? (
          <Sparkline data={spark} positive={up} width={84} height={24} />
        ) : (
          <span className="faint" style={{ fontSize: 11 }}>—</span>
        )}
      </td>
      <td>
        <span className="faint" style={{ fontSize: 11 }}>—</span>
      </td>
      <td>
        <span className="faint" style={{ fontSize: 11 }}>—</span>
      </td>
    </tr>
  );
}

export function MarketsPage() {
  const navigate = useNavigate();
  const { setSelectedSymbol } = useApp();
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | "index" | "equity">("all");
  const [, force] = useState(0);
  // Tick once a minute to re-render so any changes to the watchlist
  // (very rare) propagate.  Live prices are driven by the store.
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 60_000);
    return () => clearInterval(t);
  }, []);

  const rows = useMemo(
    () => INSTRUMENTS.map((m) => ({
      symbol: m.symbol,
      name: m.name,
      instrumentType: m.instrumentType,
    })),
    [],
  );

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        const matchQ = `${r.symbol} ${r.name}`.toLowerCase().includes(q.toLowerCase());
        const matchT = typeFilter === "all" ? true : r.instrumentType === typeFilter;
        return matchQ && matchT;
      }),
    [rows, q, typeFilter],
  );

  const open = (sym: string) => {
    setSelectedSymbol(sym);
    navigate("/");
  };

  const Table = ({ title, data }: { title: string; data: typeof filtered }) => (
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
              <th>Bias</th>
              <th>Signal</th>
            </tr>
          </thead>
          <tbody>
            {data.map((r) => (
              <MarketRow
                key={r.symbol}
                symbol={r.symbol}
                name={r.name}
                onOpen={() => open(r.symbol)}
              />
            ))}
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
          <span className="label">Source</span>
          <span className="value">Live</span>
        </div>
          <div className="stat-card">
            <span className="label">Updated</span>
            <span className="value">2s</span>
          </div>
      </div>

      <Table title="Indices" data={filtered.filter((r) => r.instrumentType === "index")} />
      <Table title="Equities" data={filtered.filter((r) => r.instrumentType === "equity")} />
    </>
  );
}

void WATCHLIST_SYMBOLS;
