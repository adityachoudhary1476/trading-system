import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { WATCHLIST_SYMBOLS } from "@/data/mock";
import { useApp } from "@/store/AppContext";
import { fmtPrice, fmtPct } from "@/lib/format";
import { useQuote } from "@/data/useQuote";

function TickerItem({ symbol }: { symbol: string }) {
  const { data: q } = useQuote(symbol);
  const navigate = useNavigate();
  const { setSelectedSymbol } = useApp();
  const [, force] = useState(0);
  // tick once a second to keep the freshness badge live (no extra fetch)
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (!q) {
    return (
      <button className="ticker-item" onClick={() => { setSelectedSymbol(symbol); navigate("/"); }}>
        <span className="tk-sym">{symbol.replace("NSE:", "")}</span>
        <span className="tk-px mono faint">—</span>
      </button>
    );
  }
  const up = q.change !== undefined ? q.change >= 0 : true;
  return (
    <button className="ticker-item" onClick={() => { setSelectedSymbol(symbol); navigate("/"); }}>
      <span className="tk-sym">{q.symbol.replace("NSE:", "")}</span>
      <span className="tk-px mono">₹{fmtPrice(q.price)}</span>
      <span className={`tk-chg ${up ? "pos" : "neg"}`}>
        {up ? "▲" : "▼"} {fmtPct(q.changePct)}
      </span>
    </button>
  );
}

/** At-a-glance ticker strip across the top of the dashboard. */
export function TickerStrip() {
  return (
    <div className="ticker" aria-label="Market ticker">
      <div className="ticker-track">
        {WATCHLIST_SYMBOLS.flatMap((s) => [
          <TickerItem key={s} symbol={s} />,
        ])}
      </div>
    </div>
  );
}
