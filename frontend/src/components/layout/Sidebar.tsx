import { NavLink } from "react-router-dom";
import { useApp, useIndianClock } from "@/store/AppContext";
import { mockQuote, WATCHLIST_SYMBOLS } from "@/data/mock";
import { dataSource } from "@/data/MarketDataSource";
import { Sparkline } from "@/components/charts/Sparkline";
import { fmtTime } from "@/lib/format";
import { useEffect, useState } from "react";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/markets", label: "Markets" },
  { to: "/signals", label: "Signals" },
  { to: "/system", label: "System" },
];

export function Header() {
  const { env } = useApp();
  const now = useIndianClock();
  const ist = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000);
  return (
    <header className="app-header">
      <button
        className="menu-btn"
        aria-label="Toggle navigation"
        onClick={() => {
          document.getElementById("sidebar")?.classList.toggle("open");
          document.getElementById("scrim")?.classList.toggle("show");
        }}
      >
        ☰
      </button>
      <div className="brand">
        <span className="brand-logo" aria-hidden="true">◧</span>
        <span className="brand-mark">FINOVA MARKETS<span className="brand-sub">AI Market Intelligence</span></span>
      </div>
      <nav className="nav" aria-label="Primary">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="header-right">
        <span
          className="mock-badge"
          title="Demo data — not connected to the market feed"
        >
          <span className="dot" /> {env.mode === "mock" ? "Demo Data" : "Live"}
        </span>
        <div className="hdr-stat">
          <span className="k">Market</span>
          <span className="v">NSE</span>
        </div>
        <div className="hdr-stat">
          <span className="k">IST</span>
          <span className="v mono">{fmtTime(ist.getTime()).slice(0, 5)}</span>
        </div>
        <div className="hdr-stat">
          <span className="k">Status</span>
          <span className="v muted">OFFLINE · MOCK</span>
        </div>
      </div>
    </header>
  );
}

function WatchRow({ sym, active, onSelect }: { sym: string; active: boolean; onSelect: (s: string) => void }) {
  const q = mockQuote(sym);
  const up = q.change >= 0;
  const [series, setSeries] = useState<number[]>([]);
  useEffect(() => {
    let alive = true;
    dataSource.getOHLCV(sym, "1D", 40).then((bars) => alive && setSeries(bars.map((b) => b.close)));
    return () => {
      alive = false;
    };
  }, [sym]);
  return (
    <button
      className={`watch-item${active ? " active" : ""}`}
      onClick={() => onSelect(sym)}
      aria-pressed={active}
    >
      <span className="watch-sym">{sym.replace("NSE:", "")}</span>
      <span className="watch-px mono">{q.price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
      <span className="watch-name">{q.name}</span>
      <span className={`watch-chg ${up ? "pos" : "neg"}`}>
        {up ? "+" : ""}
        {q.changePct.toFixed(2)}%
      </span>
      <span className="watch-spark">
        <Sparkline data={series} positive={up} width={64} height={22} />
      </span>
    </button>
  );
}

export function Sidebar() {
  const { selectedSymbol, setSelectedSymbol } = useApp();
  const now = useIndianClock();
  const ist = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000);
  const close = () => {
    document.getElementById("sidebar")?.classList.remove("open");
    document.getElementById("scrim")?.classList.remove("show");
  };
  return (
    <aside className="sidebar app-sidebar" id="sidebar">
      <div className="sidebar-section">
        <div className="sidebar-title">Watchlist</div>
        {WATCHLIST_SYMBOLS.map((sym) => (
          <WatchRow key={sym} sym={sym} active={sym === selectedSymbol} onSelect={(s) => { setSelectedSymbol(s); close(); }} />
        ))}
        <button className="add-sym" disabled title="Add symbol (coming soon)">
          + Add symbol
        </button>
      </div>
      <div className="sidebar-footer">
        <div className="sf-row">
          <span className="sf-dot" /> <span>Feed</span>
          <span className="sf-val muted">OFFLINE</span>
        </div>
        <div className="sf-row">
          <span>IST</span>
          <span className="sf-val mono">{fmtTime(ist.getTime()).slice(0, 5)}</span>
        </div>
        <div className="sf-note">Demo data · execution disabled</div>
      </div>
    </aside>
  );
}
