import { useEffect, useState } from "react";
import { useApp } from "@/store/AppContext";
import { dataSource } from "@/data/MarketDataSource";
import type { MarketStatus } from "@/types";
import { Panel } from "@/components/ui";
import { fmtTime } from "@/lib/format";

function sessionNow(): MarketStatus {
  // Mock: market is OPEN during 09:15–15:30 IST on weekdays.
  const ist = new Date(Date.now() + new Date().getTimezoneOffset() * 60000 + 5.5 * 3600000);
  const day = ist.getDay();
  const mins = ist.getHours() * 60 + ist.getMinutes();
  const weekday = day >= 1 && day <= 5;
  const regular = weekday && mins >= 555 && mins <= 930; // 09:15..15:30
  const pre = weekday && mins >= 510 && mins < 555;
  const post = weekday && mins > 930 && mins <= 960;
  const session = regular ? "REGULAR" : pre ? "PRE_MARKET" : post ? "POST_MARKET" : "CLOSED";
  return { market: "NSE", session, hours: "09:15 — 15:30 IST", open: regular };
}

export function MarketStatusPanel() {
  const { selectedSymbol } = useApp();
  const [status, setStatus] = useState<MarketStatus>(() => sessionNow());
  useEffect(() => {
    const t = setInterval(() => setStatus(sessionNow()), 30_000);
    return () => clearInterval(t);
  }, []);

  void selectedSymbol;
  const open = status.open;
  return (
    <Panel title="Market Status">
      <div className={`mkt-status-head ${open ? "open" : "closed"}`}>
        <span className="mkt-dot" />
        <span className="mkt-status-text">{open ? "MARKET OPEN" : "MARKET CLOSED"}</span>
      </div>
      <div className="grid cols-2" style={{ gap: 12, marginTop: 12 }}>
        <div className="stat">
          <span className="label">Market</span>
          <span className="value">{status.market}</span>
        </div>
        <div className="stat">
          <span className="label">Session</span>
          <span className="value">{status.session.replace("_", " ")}</span>
        </div>
        <div className="stat">
          <span className="label">Hours</span>
          <span className="value mono" style={{ fontSize: 13 }}>{status.hours}</span>
        </div>
        <div className="stat">
          <span className="label">As of</span>
          <span className="value mono" style={{ fontSize: 13 }}>{fmtTime(Date.now())}</span>
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        Mock session state. Not connected to Upstox — real backend determines state during NSE hours.
      </p>
    </Panel>
  );
}

export function useFeedHealthPanel() {
  const [health] = useState(() => dataSource.getFeedHealth());
  return health;
}
