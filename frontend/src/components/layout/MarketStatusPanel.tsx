import { Panel } from "@/components/ui";
import { fmtTimeIST } from "@/lib/format";
import { useMarketStatus } from "@/data/useQuote";

const SESSION_LABEL: Record<string, string> = {
  pre_market: "PRE-MARKET",
  regular: "REGULAR",
  post_market: "POST-MARKET",
  closed: "CLOSED",
  holiday: "HOLIDAY",
};

const SESSION_HOURS: Record<string, string> = {
  pre_market: "09:00 — 09:15 IST",
  regular: "09:15 — 15:30 IST",
  post_market: "15:30 — 16:00 IST",
  closed: "—",
  holiday: "—",
};

export function MarketStatusPanel() {
  const status = useMarketStatus();
  const phase = status?.phase ?? "closed";
  const open = phase === "regular" || phase === "pre_market" || phase === "post_market";
  const label = SESSION_LABEL[phase] ?? phase.toUpperCase();
  const hours = SESSION_HOURS[phase] ?? "—";
  return (
    <Panel title="Market Status">
      <div className={`mkt-status-head ${open ? "open" : "closed"}`}>
        <span className="mkt-dot" />
        <span className="mkt-status-text">{open ? "MARKET OPEN" : "MARKET CLOSED"}</span>
      </div>
      <div className="grid cols-2" style={{ gap: 12, marginTop: 12 }}>
        <div className="stat">
          <span className="label">Market</span>
          <span className="value">{status?.market ?? "NSE"}</span>
        </div>
        <div className="stat">
          <span className="label">Session</span>
          <span className="value">{label}</span>
        </div>
        <div className="stat">
          <span className="label">Hours</span>
          <span className="value mono" style={{ fontSize: 13 }}>{hours}</span>
        </div>
        <div className="stat">
          <span className="label">Server time (IST)</span>
          <span className="value mono" style={{ fontSize: 13 }}>
            {status?.serverTime ? fmtTimeIST(status.serverTime) : "—"}
          </span>
        </div>
      </div>
    </Panel>
  );
}
