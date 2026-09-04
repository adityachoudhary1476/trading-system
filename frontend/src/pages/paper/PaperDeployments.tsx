import { useEffect, useState, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
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
  const [showCreate, setShowCreate] = useState(false);
  const navigate = useNavigate();

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
          <span className="df-sep" />
          <Button variant="secondary" size="sm" onClick={() => setShowCreate(true)}>Create Deployment</Button>
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

      <CreateDeploymentModal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={(deploymentId) => {
          setShowCreate(false);
          fetch();
          if (deploymentId) {
            navigate(`/paper/deployments/${deploymentId}`);
          }
        }}
      />
    </div>
  );
}

const PRESET_STRATEGIES = [
  {
     key: "sma5",
     label: "SMA(5) Trend Following",
     description: "Buy when close > 5-bar SMA",
     spec: (symbol: string, timeframe: string, name: string) => ({
       name,
       description: "Buy when close exceeds the 5-period simple moving average.",
       symbol,
       timeframe,
       indicators: [{ name: "sma", params: { window: 5 } }],
       entry: {
         type: "comparison" as const,
         left: { kind: "field" as const, field: "close" },
         op: ">",
         right: { kind: "indicator" as const, indicator: "sma_5" },
       },
       allow_long: true,
       generated_by: "paper-ui",
     }),
   },
   {
     key: "sma20_50",
     label: "SMA(20/50) Crossover",
     description: "Buy when 20-bar SMA crosses above 50-bar SMA",
     spec: (symbol: string, timeframe: string, name: string) => ({
       name,
       description: "Trend-following: enter on SMA crossover.",
       symbol,
       timeframe,
       indicators: [
         { name: "sma", params: { window: 20 } },
         { name: "sma", params: { window: 50 } },
       ],
       entry: {
         type: "comparison" as const,
         left: { kind: "indicator" as const, indicator: "sma_20" },
         op: "crosses_above",
         right: { kind: "indicator" as const, indicator: "sma_50" },
       },
       allow_long: true,
       generated_by: "paper-ui",
     }),
   },
   {
     key: "rsi14",
     label: "RSI(14) Mean Reversion",
     description: "Buy when RSI(14) < 30 and close > SMA(20)",
     spec: (symbol: string, timeframe: string, name: string) => ({
       name,
       description: "Mean-reversion: buy when RSI is oversold and price is above SMA.",
       symbol,
       timeframe,
       indicators: [
         { name: "sma", params: { window: 20 } },
         { name: "rsi", params: { window: 14 } },
       ],
       entry: {
         type: "logic" as const,
         op: "AND",
         conditions: [
           {
             type: "comparison" as const,
             left: { kind: "field" as const, field: "close" },
             op: ">",
             right: { kind: "indicator" as const, indicator: "sma_20" },
           },
           {
             type: "comparison" as const,
             left: { kind: "indicator" as const, indicator: "rsi_14" },
             op: "<",
             right: { kind: "constant" as const, constant: 30 },
           },
         ],
       },
       allow_long: true,
       generated_by: "paper-ui",
     }),
   },
 ] as const;

 function CreateDeploymentModal({
   open,
   onClose,
   onCreated,
 }: {
   open: boolean;
   onClose: () => void;
   onCreated: (deploymentId: string) => void;
 }) {
   const [preset, setPreset] = useState("sma5");
   const [name, setName] = useState("My Paper Deployment");
   const [symbol, setSymbol] = useState("NSE:SBIN");
   const [timeframe, setTimeframe] = useState("1d");
   const [initialCash, setInitialCash] = useState("100000");
   const [busy, setBusy] = useState(false);
   const [submitError, setSubmitError] = useState<string | null>(null);

   const presetObj = PRESET_STRATEGIES.find((p) => p.key === preset) ?? PRESET_STRATEGIES[0];
   const spec = presetObj.spec(symbol, timeframe, name);

   const handleSubmit = useCallback(async () => {
     setSubmitError(null);
     const cash = parseFloat(initialCash);
     if (isNaN(cash) || cash <= 0) {
       setSubmitError("Initial capital must be a positive number.");
       return;
     }
     setBusy(true);
     const res = await paperApi.createDeployment({
       spec: spec as Record<string, unknown>,
       config: { initial_cash: cash },
     });
     setBusy(false);
     if (res.ok) {
       setName("My Paper Deployment");
       setSymbol("NSE:SBIN");
       setTimeframe("1d");
       setInitialCash("100000");
       onCreated(res.data.deployment.deployment_id);
     } else {
       setSubmitError(res.error.message);
     }
    }, [spec, initialCash, onCreated]);

    if (!open) return null;

    return (
      <div className="modal-overlay" onClick={onClose} role="presentation">
        <div
          className="modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="modal-title"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="modal-head" id="modal-title">Create Deployment</div>
          <div className="modal-body">
            <div className="form-group">
              <label htmlFor="deploy-name">Deployment Name</label>
              <input id="deploy-name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
            </div>
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="deploy-symbol">Symbol</label>
                <input id="deploy-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} disabled={busy} />
              </div>
              <div className="form-group">
                <label htmlFor="deploy-timeframe">Timeframe</label>
                <input id="deploy-timeframe" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} disabled={busy} />
              </div>
            </div>
            <div className="form-group">
              <label htmlFor="deploy-capital">Initial Capital</label>
              <input id="deploy-capital" type="number" min="1" step="1000" value={initialCash} onChange={(e) => setInitialCash(e.target.value)} disabled={busy} />
            </div>
            <div className="form-group">
              <label htmlFor="deploy-strategy">Strategy Preset</label>
              <select id="deploy-strategy" value={preset} onChange={(e) => setPreset(e.target.value)} disabled={busy}>
                {PRESET_STRATEGIES.map((p) => (
                  <option key={p.key} value={p.key}>{p.label} — {p.description}</option>
                ))}
              </select>
            </div>
            {submitError && (
              <div className="fb-error feedback" role="alert">{submitError}</div>
            )}
          </div>
          <div className="modal-foot">
            <Button variant="secondary" size="sm" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button variant="primary" size="sm" onClick={handleSubmit} disabled={busy}>
              {busy ? "Creating…" : "Create"}
            </Button>
          </div>
        </div>
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
