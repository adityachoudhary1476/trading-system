import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { PositionsResponse } from "@/types/paper-api";
import { Panel, EmptyState, Loading, StatusIndicator, MetricItem } from "@/components/ui";
import { DeploymentPicker, fmt } from "@/components/paper/paperShared";

export function PaperPositions() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [positions, setPositions] = useState<PositionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  useEffect(() => {
    if (!deploymentIdState) return;
    let alive = true;
    setLoading(true);
    setError(null);
    paperApi.getPositions(deploymentIdState).then((res) => {
      if (!alive) return;
      if (res.ok) {
        setPositions(res.data);
      } else {
        setError(res.error.message);
      }
      setLoading(false);
    });
    return () => { alive = false; };
  }, [deploymentIdState]);

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Select Deployment</h2></div>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view its positions…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its open positions."
        />
      </div>
    );
  }

  return (
    <div className="paper-shell">
      {loading && <Loading label="Loading positions…" />}

      {error && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load positions</div>
          <div className="es-hint">{error}</div>
        </div>
      )}

      {positions && (
        <>
          <div className="pt-section">
            <h2>Positions</h2>
            <span className="section-sub">{positions.positions.is_flat ? "Flat" : "1 open"}</span>
          </div>

          {positions.positions.is_flat || !positions.positions.open_position ? (
            <Panel subtle>
              <EmptyState
                title="No open positions"
                hint={`Deployment ${deploymentIdState} is currently flat.`}
              />
            </Panel>
          ) : (
            <>
              {/* Position Summary */}
              <Panel>
                <div className="metric-grid">
                  <MetricItem label="Symbol" value={positions.positions.open_position.symbol} />
                  <MetricItem label="Side" value={positions.positions.open_position.side.toUpperCase()} tone={positions.positions.open_position.side === "long" ? "pos" : "neg"} />
                  <MetricItem label="Quantity" value={positions.positions.open_position.quantity.toLocaleString("en-IN", { maximumFractionDigits: 2 })} />
                  <MetricItem label="Entry Price" value={fmt.currency(positions.positions.open_position.entry_price)} />
                  <MetricItem label="Current Price" value={fmt.currency(positions.positions.open_position.current_price)} />
                  <MetricItem label="Unrealized P&L" value={fmt.currency(positions.positions.open_position.unrealized_pnl)} tone={positions.positions.open_position.unrealized_pnl >= 0 ? "pos" : "neg"} />
                  <MetricItem label="Position Value" value={fmt.currency(positions.positions.open_position.position_value)} />
                </div>
              </Panel>

              {/* Position Table */}
              <Panel title="Position Detail" className="mt-md">
                <table className="data dense">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th className="num">Quantity</th>
                      <th className="num">Entry Price</th>
                      <th className="num">Current Price</th>
                      <th className="num">Unrealized P&L</th>
                      <th className="num">Position Value</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr className={positions.positions.open_position.unrealized_pnl >= 0 ? "" : "row-warning"}>
                      <td>{positions.positions.open_position.symbol}</td>
                      <td><StatusIndicator status={positions.positions.open_position.side === "long" ? "healthy" : "stopped"} /></td>
                      <td className="num">{positions.positions.open_position.quantity.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</td>
                      <td className="num">{fmt.currency(positions.positions.open_position.entry_price)}</td>
                      <td className="num">{fmt.currency(positions.positions.open_position.current_price)}</td>
                      <td className={`num ${positions.positions.open_position.unrealized_pnl >= 0 ? "pos" : "neg"}`}>
                        {fmt.currency(positions.positions.open_position.unrealized_pnl)}
                      </td>
                      <td className="num">{fmt.currency(positions.positions.open_position.position_value)}</td>
                    </tr>
                  </tbody>
                </table>
              </Panel>
            </>
          )}

          {/* Navigation */}
          <Panel title="Navigate" className="mt-md">
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
