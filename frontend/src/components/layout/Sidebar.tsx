import { NavLink, useLocation } from "react-router-dom";
import { useApp, useIndianClock } from "@/store/AppContext";
import { mockQuote, WATCHLIST_SYMBOLS } from "@/data/mock";
import { dataSource } from "@/data/MarketDataSource";
import { Sparkline } from "@/components/charts/Sparkline";
import { fmtTime, fmtPrice } from "@/lib/format";
import { useEffect, useState } from "react";

const MAIN_NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/markets", label: "Markets" },
  { to: "/signals", label: "Signals" },
  { to: "/system", label: "System" },
  { to: "/broker", label: "Broker" },
];

const PAPER_NAV = [
  { to: "/paper/overview", label: "Overview", icon: "◧" },
];

const PAPER_OPS = [
  { to: "/paper/deployments", label: "Deployments", icon: "▦" },
  { to: "/paper/strategies", label: "Strategies", icon: "⟡" },
  { to: "/paper/positions", label: "Positions", icon: "◫" },
  { to: "/paper/sessions", label: "Sessions", icon: "◷" },
];

const PAPER_MONITORING = [
  { to: "/paper/events", label: "Events", icon: "☰" },
  { to: "/paper/risk", label: "Risk & Health", icon: "◔" },
];

const PAPER_REPORTING = [
  { to: "/paper/reports", label: "Reports", icon: "▤" },
  { to: "/paper/research", label: "Research", icon: "✺" },
];

export function Header() {
  const { env } = useApp();
  const now = useIndianClock();
  const ist = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000);
  const location = useLocation();
  const isPaper = location.pathname.startsWith("/paper");

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
        {MAIN_NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.label}
          </NavLink>
        ))}
        <NavLink
          to="/paper/overview"
          className={({ isActive }) => isActive ? "active" : undefined}
        >
          Paper Trading
        </NavLink>
      </nav>
      <div className="header-right">
        {isPaper && (
          <div className="header-context" role="status">
            <span className="hc-dot" aria-hidden="true" />
            <span className="hc-label">Paper</span>
            <span className="hc-sep" />
            <span>Simulation</span>
          </div>
        )}
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
      </div>
    </header>
  );
}

function WatchRow({ sym, active, onSelect }: { sym: string; active: boolean; onSelect: (s: string) => void }) {
  const q = mockQuote(sym);
  const up = q.change !== undefined ? q.change >= 0 : true;
  const [series, setSeries] = useState<number[]>([]);
  useEffect(() => {
    let alive = true;
    dataSource
      .getOHLCV(sym, "1D", 40)
      .then((bars) => {
        if (alive) setSeries(bars.map((b) => b.close));
      })
      .catch(() => {
        if (alive) setSeries([]);
      });
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
      <span className="watch-px mono">₹{fmtPrice(q.price)}</span>
      <span className="watch-name">{q.name}</span>
      <span className={`watch-chg ${up ? "pos" : "neg"}`}>
        {up ? "+" : ""}
        {q.changePct !== undefined ? `${q.changePct.toFixed(2)}%` : "—"}
      </span>
      <span className="watch-spark">
        <Sparkline data={series} positive={up} width={64} height={22} />
      </span>
    </button>
  );
}

function PaperNavGroup({ title, items }: { title: string; items: typeof PAPER_OPS }) {
  return (
    <div className="paper-nav-section">
      <div className="paper-nav-section-title">{title}</div>
      {items.map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.to === "/paper/overview"}
          className={({ isActive }) => `paper-nav-item${isActive ? " active" : ""}`}
        >
          <span className="nav-icon" aria-hidden="true">{n.icon}</span>
          {n.label}
        </NavLink>
      ))}
    </div>
  );
}

export function Sidebar() {
  const { selectedSymbol, setSelectedSymbol } = useApp();
  const now = useIndianClock();
  const ist = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000);
  const location = useLocation();
  const isPaper = location.pathname.startsWith("/paper");

  const close = () => {
    document.getElementById("sidebar")?.classList.remove("open");
    document.getElementById("scrim")?.classList.remove("show");
  };

  return (
    <aside className="sidebar app-sidebar" id="sidebar">
      {isPaper ? (
        <div className="paper-nav">
          <div className="paper-nav-section">
            <div className="paper-nav-section-title">Paper Trading</div>
            {PAPER_NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/paper/overview"}
                className={({ isActive }) => `paper-nav-item${isActive ? " active" : ""}`}
                onClick={close}
              >
                <span className="nav-icon" aria-hidden="true">{n.icon}</span>
                {n.label}
              </NavLink>
            ))}
          </div>
          <div className="sidebar-divider" />
          <PaperNavGroup title="Operations" items={PAPER_OPS} />
          <div className="sidebar-divider" />
          <PaperNavGroup title="Monitoring" items={PAPER_MONITORING} />
          <div className="sidebar-divider" />
          <PaperNavGroup title="Reporting" items={PAPER_REPORTING} />
        </div>
      ) : (
        <div className="sidebar-section">
          <div className="sidebar-title">Watchlist</div>
          {WATCHLIST_SYMBOLS.map((sym) => (
            <WatchRow key={sym} sym={sym} active={sym === selectedSymbol} onSelect={(s) => { setSelectedSymbol(s); close(); }} />
          ))}
          <button className="add-sym" disabled title="Add symbol (coming soon)">
            + Add symbol
          </button>
        </div>
      )}
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
