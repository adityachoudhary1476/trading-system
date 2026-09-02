import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { WATCHLIST_SYMBOLS, mockQuote } from "@/data/mock";
import { useApp } from "@/store/AppContext";
import { fmtPrice, fmtPct } from "@/lib/format";

/** At-a-glance ticker strip across the top of the dashboard. */
export function TickerStrip() {
  const navigate = useNavigate();
  const { setSelectedSymbol } = useApp();
  const [quotes, setQuotes] = useState(() => WATCHLIST_SYMBOLS.map(mockQuote));

  // Static deterministic refresh (no fake "live" claim) — once on mount.
  useEffect(() => {
    setQuotes(WATCHLIST_SYMBOLS.map(mockQuote));
  }, []);

  const open = (sym: string) => {
    setSelectedSymbol(sym);
    navigate("/");
  };

  return (
    <div className="ticker" aria-label="Market ticker">
      <div className="ticker-track">
        {[...quotes, ...quotes].map((q, i) => {
          const up = q.change !== undefined ? q.change >= 0 : true;
          return (
            <button key={`${q.symbol}-${i}`} className="ticker-item" onClick={() => open(q.symbol)}>
              <span className="tk-sym">{q.symbol.replace("NSE:", "")}</span>
              <span className="tk-px mono">₹{fmtPrice(q.price)}</span>
              <span className={`tk-chg ${up ? "pos" : "neg"}`}>
                {up ? "▲" : "▼"} {fmtPct(q.changePct)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
