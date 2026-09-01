import { Outlet } from "react-router-dom";

export function PaperTradingPage() {
  return (
    <div className="paper-shell">
      <div className="page-head">
        <div>
          <h1 className="page-title">Paper Trading</h1>
          <p className="subtitle">
            Simulated execution environment. No real money at risk.
          </p>
        </div>
        <span className="shell-env" title="Paper-only environment — no live broker connection">
          <span className="env-dot" aria-hidden="true" /> PAPER ENVIRONMENT
        </span>
      </div>
      <Outlet />
    </div>
  );
}
