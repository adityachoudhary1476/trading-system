import { NavLink } from "react-router-dom";
import { useApp, useIndianClock } from "@/store/AppContext";
import { mockQuote, WATCHLIST_SYMBOLS } from "@/data/mock";
import { fmtTime } from "@/lib/format";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/markets", label: "Markets" },
  { to: "/signals", label: "Signals" },
  { to: "/system", label: "System" },
];

export function Header() {
  const { env } = useApp();
  const now = useIndianClock();
  const ist = new Date(now.getTime() + (now.getTimezoneOffset() * 60000) + 5.5 * 3600000);
  return (
    <header className="app-header">
      <button className="menu-btn" aria-label="Toggle navigation" onClick={() => {
        const sb = document.getElementById("sidebar");
        sb?.classList.toggle("open");
        document.getElementById("scrim")?.classList.toggle("show");
      }}>☰</button>
      <div className="brand">
        <span className="brand-mark">FINOVA MARKETS</span>
        <span className="brand-sub">AI Market Intelligence</span>
      </div>
      <nav className="nav" aria-label="Primary">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="header-right">
        <span className="mock-badge" title="All data on this terminal is simulated demo data; not connected to a live market feed.">
          <span className="dot" /> {env.mode === "mock" ? "Demo Data" : "Live"}
        </span>
        <div className="hdr-stat">
          <span className="k">Market</span>
          <span className="v">NSE</span>
        </div>
        <div className="hdr-stat">
          <span className="k">IST Time</span>
          <span className="v mono">{fmtTime(ist.getTime()).slice(0, 5)}</span>
        </div>
        <div className="hdr-stat">
          <span className="k">Connection</span>
          <span className="v" style={{ color: "var(--text-dim)" }}>OFFLINE · MOCK</span>
        </div>
      </div>
    </header>
  );
}

export function Sidebar() {
  const { selectedSymbol, setSelectedSymbol } = useApp();
  return (
    <aside className="sidebar app-sidebar" id="sidebar">
      <div className="sidebar-section">
        <div className="sidebar-title">Watchlist</div>
        {WATCHLIST_SYMBOLS.map((sym) => {
          const q = mockQuote(sym);
          const up = q.change >= 0;
          return (
            <button
              key={sym}
              className={`watch-item${sym === selectedSymbol ? " active" : ""}`}
              onClick={() => {
                setSelectedSymbol(sym);
                document.getElementById("sidebar")?.classList.remove("open");
                document.getElementById("scrim")?.classList.remove("show");
              }}
              aria-pressed={sym === selectedSymbol}
            >
              <span className="watch-sym">{sym.replace("NSE:", "")}</span>
              <span className={`watch-px mono`}>{q.price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span>
              <span className="watch-name">{q.name}</span>
              <span className={`watch-chg ${up ? "pos" : "neg"}`}>{up ? "+" : ""}{q.changePct.toFixed(2)}%</span>
            </button>
          );
        })}
        <button className="add-sym" disabled title="Add symbol (coming soon)">+ Add symbol</button>
      </div>
    </aside>
  );
}
