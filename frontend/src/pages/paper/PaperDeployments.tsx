import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { DeploymentListResponse } from "@/types/paper-api";
import { Panel, EmptyState, Button, StatusIndicator } from "@/components/ui";

export function PaperDeployments() {
  const [list, setList] = useState<DeploymentListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState({
    symbol: "",
    timeframe: "",
    status: "",
  });
  const [view, setView] = useState<"cards" | "table">("cards");

  const fetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    const res = await paperApi.listDeployments({
      symbol: filters.symbol || undefined,
      timeframe: filters.timeframe || undefined,
      status: filters.status || undefined,
      limit: 200,
    });
    if (res.ok) {
      setList(res.data);
    } else {
      setError(res.error.message);
    }
    setLoading(false);
  }, [filters]);

  useEffect(() => {
    fetch();
  }, []); // initial load only — apply is manual

  const updateFilter = (key: string, value: string) => {
    setFilters((f) => ({ ...f, [key]: value }));
  };

  const hasFilters = filters.symbol || filters.timeframe || filters.status;

  return (
    <div className="paper-shell">
      <div className="page-head">
        <div>
          <h1 className="page-title">Paper Deployments</h1>
          <p className="subtitle">
            Operational list of paper deployments. No real money at risk.
          </p>
        </div>
        <span className="shell-env" title="Paper-only environment — no live broker connection">
          <span className="env-dot" aria-hidden="true" /> PAPER ENVIRONMENT
        </span>
      </div>

      <div className="deploy-filter-bar">
        <div className="df-field">
          <label className="df-label" htmlFor="df-symbol">Symbol</label>
          <input
            id="df-symbol"
            className="search"
            placeholder="e.g. NSE:SBIN"
            value={filters.symbol}
            onChange={(e) => updateFilter("symbol", e.target.value)}
          />
        </div>
        <div className="df-field">
          <label className="df-label" htmlFor="df-timeframe">Timeframe</label>
          <input
            id="df-timeframe"
            className="search"
            placeholder="e.g. 1d"
            value={filters.timeframe}
            onChange={(e) => updateFilter("timeframe", e.target.value)}
          />
        </div>
        <div className="df-field">
          <label className="df-label" htmlFor="df-status">Status</label>
          <input
            id="df-status"
            className="search"
            placeholder="e.g. active"
            value={filters.status}
            onChange={(e) => updateFilter("status", e.target.value)}
          />
        </div>
        <div className="df-actions">
          <Button variant="primary" size="sm" onClick={fetch}>Apply</Button>
          {hasFilters && (
            <Button variant="ghost" size="sm" onClick={() => { setFilters({ symbol: "", timeframe: "", status: "" }); }}>Clear</Button>
          )}
          <span className="df-sep" />
          <Button variant={view === "cards" ? "secondary" : "ghost"} size="xs" onClick={() => setView("cards")}>Cards</Button>
          <Button variant={view === "table" ? "secondary" : "ghost"} size="xs" onClick={() => setView("table")}>Table</Button>
        </div>
      </div>

      <div className="deploy-count">
        {list ? `${list.count} deployment${list.count === 1 ? "" : "s"}` : ""}
      </div>

      {loading && <DeploymentsSkeleton view={view} />}

      {error && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load deployments</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={fetch}>Retry</Button>
        </div>
      )}

      {!loading && !error && list && list.deployments.length === 0 && (
        <Panel subtle>
          <EmptyState
            title="No deployments"
            hint={hasFilters ? "No deployments match the current filters." : "No deployments have been created yet."}
          />
        </Panel>
      )}

      {!loading && !error && list && list.deployments.length > 0 && view === "cards" && (
        <div className="deploy-grid">
          {list.deployments.map((d) => (
            <DeploymentCard key={d.deployment_id} deployment={d} />
          ))}
        </div>
      )}

      {!loading && !error && list && list.deployments.length > 0 && view === "table" && (
        <Panel subtle>
          <table className="data dense">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Symbol</th>
                <th>Timeframe</th>
                <th>Status</th>
                <th>Mode</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.deployments.map((d) => (
                <tr key={d.deployment_id} className={d.status === "failed" || d.status === "stopped" ? "row-critical" : d.status === "paused" ? "row-warning" : ""}>
                  <td>
                    <Link to={`/paper/deployments/${d.deployment_id}`} className="td-id">
                      {d.strategy_id}
                    </Link>
                  </td>
                  <td>{d.symbol}</td>
                  <td>{d.timeframe}</td>
                  <td><StatusIndicator status={d.status} /></td>
                  <td className="td-muted">{d.execution_mode}</td>
                  <td className="td-muted">{d.updated_at?.slice(0, 10) ?? "—"}</td>
                  <td className="td-num">
                    <Link to={`/paper/deployments/${d.deployment_id}`} className="btn btn-ghost btn-xs">View</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

function DeploymentCard({ deployment: d }: { deployment: DeploymentListResponse["deployments"][number] }) {
  const isCritical = d.status === "failed" || d.status === "stopped";
  const isWarning = d.status === "paused";

  return (
    <Link to={`/paper/deployments/${d.deployment_id}`} className="full" style={{ textDecoration: "none", color: "inherit" }}>
      <div className={`deploy-card${isCritical ? " dc-critical" : isWarning ? " dc-warning" : ""}`}>
        <div className="dc-head">
          <div>
            <span className="dc-name">{d.strategy_id}</span>
            <div className="dc-meta">{d.symbol} · {d.timeframe}</div>
          </div>
          <StatusIndicator status={d.status} />
        </div>
        <div className="dc-status-row">
          <span className="pill">{d.execution_mode.toUpperCase()}</span>
          <span className="pill muted">Updated {d.updated_at?.slice(0, 10) ?? "—"}</span>
        </div>
        <div className="dc-foot">
          <span className="section-sub">View deployment →</span>
        </div>
      </div>
    </Link>
  );
}

function DeploymentsSkeleton({ view }: { view: "cards" | "table" }) {
  if (view === "cards") {
    return (
      <div className="deploy-grid" aria-label="Loading deployments">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skel skel-block" style={{ height: 120 }} />
        ))}
      </div>
    );
  }
  return (
    <Panel subtle>
      <div className="skel skel-block" style={{ height: 240 }} />
    </Panel>
  );
}
