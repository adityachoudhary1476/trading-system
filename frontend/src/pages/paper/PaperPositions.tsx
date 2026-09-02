import { useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { PositionsResponse, DeploymentResponse } from "@/types/paper-api";
import { Panel, EmptyState, Button, StatusIndicator, MetricItem } from "@/components/ui";
import { DeploymentPicker, fmt } from "@/components/paper/paperShared";
import { fmtNum } from "@/lib/format";

export function PaperPositions() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [positions, setPositions] = useState<PositionsResponse | null>(null);
  const [deployment, setDeployment] = useState<DeploymentResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    if (!deploymentIdState) return;
    setLoading(true);
    setError(null);
    const [posRes, depRes] = await Promise.all([
      paperApi.getPositions(deploymentIdState),
      paperApi.getDeployment(deploymentIdState).catch(() => null),
    ]);
    if (posRes.ok) {
      setPositions(posRes.data);
    } else {
      setError(posRes.error.message);
      setPositions(null);
    }
    if (depRes && depRes.ok) {
      setDeployment(depRes.data);
    }
    setLoading(false);
  }, [deploymentIdState]);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Paper Positions</h2></div>
        <p className="faint" style={{ fontSize: 11.5, marginTop: -8, marginBottom: 8 }}>
          Positions show current open exposure for a paper deployment. Select a deployment to inspect its position state.
        </p>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view its positions…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its open paper positions."
        />
      </div>
    );
  }

  const dep = deployment?.deployment;
  const pos = positions?.positions;
  const isFlat = pos?.is_flat ?? true;
  const open = pos?.open_position ?? null;

  return (
    <div className="paper-shell">
      {loading && <PositionsSkeleton />}

      {error && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load positions</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={fetchAll}>Retry</Button>
        </div>
      )}

      {!loading && !error && dep && positions && (
        <>
          {/* Header */}
          <div className="detail-head">
            <div className="dh-top">
              <div>
                <div className="dh-title">
                  {dep.strategy_id}
                </div>
                <div className="dh-sub">
                  {dep.deployment_id} · {dep.symbol} · {dep.timeframe}
                </div>
              </div>
              <div className="row gap-sm wrap">
                <span className="pill">{dep.execution_mode.toUpperCase()}</span>
                <StatusIndicator status={dep.status} />
              </div>
            </div>
            <div className="dh-metrics">
              <div className="dh-metric">
                <span className="m-label">Mode</span>
                <span className="m-value">{dep.execution_mode.toUpperCase()}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Symbol</span>
                <span className="m-value">{dep.symbol}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Timeframe</span>
                <span className="m-value">{dep.timeframe}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Status</span>
                <span className="m-value"><StatusIndicator status={dep.status} /></span>
              </div>
            </div>
          </div>

          {/* Position State Banner */}
          <div className={`pos-banner${isFlat ? " pos-flat" : " pos-open"}`}>
            <div className="pos-banner-top">
              <StatusIndicator status={isFlat ? "stopped" : "active"} />
              <span className="pos-banner-status">
                {isFlat ? "FLAT" : open?.side?.toUpperCase()}
              </span>
              {!isFlat && open && (
                <span className="pill">{open.side}</span>
              )}
            </div>
            {!isFlat && open && (
              <div className="pos-banner-metrics">
                <div className="pos-banner-metric">
                  <span className="m-label">Quantity</span>
                  <span className="m-value">{fmtNum(open.quantity, { maximumFractionDigits: 2 })}</span>
                </div>
                <div className="pos-banner-metric">
                  <span className="m-label">Unrealized P&L</span>
                  <span className={`m-value${open.unrealized_pnl >= 0 ? " pos" : " neg"}`}>{fmt.currency(open.unrealized_pnl)}</span>
                </div>
                <div className="pos-banner-metric">
                  <span className="m-label">Position Value</span>
                  <span className="m-value">{fmt.currency(open.position_value)}</span>
                </div>
              </div>
            )}
          </div>

          {/* Position Detail */}
          {!isFlat && open ? (
            <Panel title="Position Detail">
              <div className="metric-grid">
                <MetricItem label="Symbol" value={open.symbol} />
                <MetricItem label="Side" value={open.side.toUpperCase()} tone={open.side === "long" ? "pos" : "neg"} />
                <MetricItem label="Quantity" value={fmtNum(open.quantity, { maximumFractionDigits: 2 })} />
                <MetricItem label="Entry Price" value={fmt.currency(open.entry_price)} />
                <MetricItem label="Current Price" value={fmt.currency(open.current_price)} />
                <MetricItem label="Position Value" value={fmt.currency(open.position_value)} />
                <MetricItem label="Unrealized P&L" value={fmt.currency(open.unrealized_pnl)} tone={open.unrealized_pnl >= 0 ? "pos" : "neg"} />
              </div>
            </Panel>
          ) : (
            <Panel subtle>
              <EmptyState
                title="No open position"
                hint={`Deployment ${deploymentIdState} is currently flat. No paper position is open.`}
              />
            </Panel>
          )}

          {/* Navigation */}
          <Panel title="Navigate">
            <div className="controls-group">
              <Link to={`/paper/deployments/${deploymentIdState}`} className="btn btn-secondary btn-sm">Deployment Detail</Link>
              <Link to={`/paper/sessions/${deploymentIdState}`} className="btn btn-secondary btn-sm">Session</Link>
              <Link to={`/paper/events/${deploymentIdState}`} className="btn btn-secondary btn-sm">Events</Link>
              <Link to={`/paper/risk/${deploymentIdState}`} className="btn btn-secondary btn-sm">Risk & Health</Link>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}

function PositionsSkeleton() {
  return (
    <div className="stack gap-lg" aria-label="Loading positions">
      <div className="skel skel-block" style={{ height: 120 }} />
      <div className="skel skel-block" style={{ height: 80 }} />
      <div className="skel skel-block" style={{ height: 160 }} />
      <div className="skel skel-block" style={{ height: 100 }} />
    </div>
  );
}
