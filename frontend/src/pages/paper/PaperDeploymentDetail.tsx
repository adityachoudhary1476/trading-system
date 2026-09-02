import { useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { DeploymentResponse, DashboardSnapshotResponse } from "@/types/paper-api";
import { Panel, EmptyState, Button, StatusIndicator, MetricItem, Feedback, ConfirmDialog } from "@/components/ui";
import { fmt } from "@/components/paper/paperShared";
import { fmtNum } from "@/lib/format";

export function PaperDeploymentDetail() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deployment, setDeployment] = useState<DeploymentResponse | null>(null);
  const [snapshot, setSnapshot] = useState<DashboardSnapshotResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    if (!deploymentId) return;
    setLoading(true);
    setError(null);
    const [depRes, dashRes] = await Promise.all([
      paperApi.getDeployment(deploymentId),
      paperApi.getDashboard(deploymentId).catch(() => null),
    ]);
    if (depRes.ok) {
      setDeployment(depRes.data);
    } else {
      setError(depRes.error.message);
    }
    if (dashRes && dashRes.ok) {
      setSnapshot(dashRes.data);
    }
    setLoading(false);
  }, [deploymentId]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  if (!deploymentId) return <EmptyState title="No deployment selected" hint="Select a deployment from the Deployments page." />;

  const isCritical = deployment?.deployment.status === "failed" || snapshot?.circuit_breaker.state === "open";
  const isHalted = snapshot?.health.status === "halted";

  return (
    <div className="paper-shell">
      <div className="pt-section">
        <Link to="/paper/deployments" className="section-sub">← Paper Deployments</Link>
      </div>

      {loading && <DetailSkeleton />}

      {error && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load deployment</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={fetchAll}>Retry</Button>
        </div>
      )}

      {deployment && snapshot && !loading && (
        <>
          {/* Deployment Header */}
          <div className={`detail-head${isCritical ? " circuit-open" : ""}`}>
            <div className="dh-top">
              <div>
                <div className="dh-title">
                  {deployment.deployment.strategy_id}
                </div>
                <div className="dh-sub">
                  {deployment.deployment.deployment_id} · {deployment.deployment.symbol} · {deployment.deployment.timeframe}
                </div>
              </div>
              <div className="row gap-sm wrap">
                <span className="pill">{deployment.deployment.execution_mode.toUpperCase()}</span>
                <StatusIndicator status={deployment.deployment.status} />
                <StatusIndicator status={snapshot.session.session_status} />
              </div>
            </div>
            <div className="dh-metrics">
              <div className="dh-metric">
                <span className="m-label">Equity</span>
                <span className="m-value">{fmt.currency(snapshot.account.equity)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Realized P&L</span>
                <span className={`m-value${(snapshot.account.realized_pnl ?? 0) >= 0 ? " pos" : " neg"}`}>{fmt.currency(snapshot.account.realized_pnl)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Return</span>
                <span className={`m-value${(snapshot.performance.return ?? 0) >= 0 ? " pos" : " neg"}`}>{fmt.pct(snapshot.performance.return)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Drawdown</span>
                <span className="m-value neg">{fmt.pctPlain(snapshot.performance.drawdown)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Bars</span>
                <span className="m-value">{fmt.num(snapshot.performance.bar_count)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Orders</span>
                <span className="m-value">{fmt.num(snapshot.performance.orders_submitted)}</span>
              </div>
            </div>
          </div>

          {/* Critical State Banner */}
          {isCritical && (
            <div className="risk-block rb-critical" role="alert">
              <div className="rb-top">
                <StatusIndicator status={isHalted ? "halted" : "failed"} />
                <span className="rb-status">{isHalted ? "HALTED" : "FAILED"}</span>
              </div>
              <div className="rb-reason">
                {isHalted && (snapshot.health.halt_reason || "Paper execution is currently blocked.")}
                {!isHalted && "Deployment has failed."}
              </div>
            </div>
          )}

          {/* Attention / Safety Panel */}
          <Panel title="Safety State" className={snapshot.circuit_breaker.state === "open" ? "circuit-open" : undefined}>
            <div className="grid cols-3" style={{ gap: 12 }}>
              <div>
                <div className="cg-label">Health</div>
                <StatusIndicator status={snapshot.health.status} />
                {snapshot.health.halt_reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{snapshot.health.halt_reason}</div>}
                {snapshot.health.warnings.length > 0 && (
                  <ul className="tag-list mt-sm">
                    {snapshot.health.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                )}
              </div>
              <div>
                <div className="cg-label">Risk Decision</div>
                <StatusIndicator status={snapshot.risk.decision === "allow" ? "active" : snapshot.risk.decision === "warning" ? "paused" : "stopped"} />
                {snapshot.risk.reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{snapshot.risk.reason}</div>}
              </div>
              <div>
                <div className="cg-label">Circuit Breaker</div>
                <div className="row gap-sm">
                  <StatusIndicator status={snapshot.circuit_breaker.state === "open" ? "open" : "closed"} />
                  <span className="pill">Trips: {snapshot.circuit_breaker.trip_count}</span>
                </div>
                {snapshot.circuit_breaker.reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{snapshot.circuit_breaker.reason}</div>}
              </div>
            </div>
          </Panel>

          {/* Performance */}
          <Panel title="Performance">
            <div className="metric-grid">
              <MetricItem label="Starting Equity" value={fmt.currency(snapshot.account.starting_equity)} />
              <MetricItem label="Equity" value={fmt.currency(snapshot.account.equity)} tone="pos" />
              <MetricItem label="Realized P&L" value={fmt.currency(snapshot.account.realized_pnl)} tone={(snapshot.account.realized_pnl ?? 0) >= 0 ? "pos" : "neg"} />
              <MetricItem label="Unrealized P&L" value={fmt.currency(snapshot.account.unrealized_pnl)} tone={(snapshot.account.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg"} />
              <MetricItem label="Return" value={fmt.pct(snapshot.performance.return)} tone={(snapshot.performance.return ?? 0) >= 0 ? "pos" : "neg"} />
              <MetricItem label="Drawdown" value={fmt.pctPlain(snapshot.performance.drawdown)} tone="neg" />
              <MetricItem label="Trade Count" value={String(snapshot.performance.trade_count)} />
              <MetricItem label="Win Rate" value={snapshot.performance.win_rate === null ? "—" : `${(snapshot.performance.win_rate * 100).toFixed(1)}%`} />
              <MetricItem label="Profit Factor" value={snapshot.performance.profit_factor?.toFixed(2) ?? "—"} />
            </div>
          </Panel>

          {/* Operations */}
          <Panel title="Operations">
            <div className="metric-grid">
              <MetricItem label="Bars Processed" value={fmt.num(snapshot.performance.bar_count)} />
              <MetricItem label="Signals Generated" value={fmt.num(snapshot.performance.generated_signals)} />
              <MetricItem label="Orders Submitted" value={fmt.num(snapshot.performance.orders_submitted)} />
              <MetricItem label="Fills Received" value={fmt.num(snapshot.performance.fills_received)} />
              <MetricItem label="Rejected Orders" value={String(snapshot.performance.rejected_orders)} tone={snapshot.performance.rejected_orders > 0 ? "neg" : undefined} />
              <MetricItem label="Consecutive Errors" value={String(snapshot.session.consecutive_errors)} tone={snapshot.session.consecutive_errors > 0 ? "neg" : undefined} />
              <MetricItem label="Event Count" value={fmt.num(snapshot.session.event_count)} />
            </div>
          </Panel>

          {/* Positions */}
          <div>
            <div className="pt-section">
              <h2>Positions</h2>
              <Link to={`/paper/positions/${deploymentId}`} className="section-sub">Details ›</Link>
            </div>
            {snapshot.positions.is_flat || !snapshot.positions.open_position ? (
              <Panel subtle>
                <EmptyState title="No open positions" hint="The paper account currently has no open positions." />
              </Panel>
            ) : (
              <Panel subtle>
                <table className="data dense">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th className="num">Qty</th>
                      <th className="num">Entry</th>
                      <th className="num">Current</th>
                      <th className="num">Unreal. P&L</th>
                      <th className="num">Value</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr className={snapshot.positions.open_position.unrealized_pnl >= 0 ? "" : "row-warning"}>
                      <td>{snapshot.positions.open_position.symbol}</td>
                      <td><StatusIndicator status={snapshot.positions.open_position.side === "long" ? "active" : "stopped"} /></td>
                      <td className="num">{fmtNum(snapshot.positions.open_position.quantity, { useGrouping: true })}</td>
                      <td className="num">{fmt.currency(snapshot.positions.open_position.entry_price)}</td>
                      <td className="num">{fmt.currency(snapshot.positions.open_position.current_price)}</td>
                      <td className={`num ${snapshot.positions.open_position.unrealized_pnl >= 0 ? "pos" : "neg"}`}>{fmt.currency(snapshot.positions.open_position.unrealized_pnl)}</td>
                      <td className="num">{fmt.currency(snapshot.positions.open_position.position_value)}</td>
                    </tr>
                  </tbody>
                </table>
              </Panel>
            )}
          </div>

          {/* Recent Events */}
          <div>
            <div className="pt-section">
              <h2>Recent Activity</h2>
              <Link to={`/paper/events/${deploymentId}`} className="section-sub">View all ›</Link>
            </div>
            {snapshot.recent_events.recent.length === 0 ? (
              <Panel subtle>
                <EmptyState title="No recent events" hint="Events will appear here as the deployment processes data." />
              </Panel>
            ) : (
              <Panel subtle>
                <div className="event-log">
                  {[...snapshot.recent_events.recent].reverse().slice(0, 8).map((ev) => (
                    <div key={ev.sequence} className={`el-row${isCriticalEvent(ev.event_type) ? " el-critical" : isWarningEvent(ev.event_type) ? " el-warn" : " el-info"}`}>
                      <span className="el-seq">#{ev.sequence}</span>
                      <span className="el-time">{ev.timestamp ? ev.timestamp.slice(11, 19) : "—"}</span>
                      <span className="el-type">{ev.event_type.replace(/_/g, " ")}</span>
                      <span className="el-msg">{ev.message}</span>
                    </div>
                  ))}
                </div>
              </Panel>
            )}
          </div>

          {/* Lifecycle Controls */}
          <LifecycleControls deploymentId={deploymentId} status={deployment.deployment.status} onRefresh={fetchAll} />
        </>
      )}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="stack gap-lg" aria-label="Loading deployment detail">
      <div className="skel skel-block" style={{ height: 100 }} />
      <div className="skel skel-block" style={{ height: 80 }} />
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div className="skel skel-block" style={{ height: 200 }} />
        <div className="skel skel-block" style={{ height: 200 }} />
      </div>
      <div className="skel skel-block" style={{ height: 180 }} />
      <div className="skel skel-block" style={{ height: 160 }} />
      <div className="skel skel-block" style={{ height: 180 }} />
    </div>
  );
}

function LifecycleControls({ deploymentId, status, onRefresh }: { deploymentId: string; status: string; onRefresh: () => void }) {
  const [feedback, setFeedback] = useState<{ kind: "success" | "error" | "processing"; msg: string } | null>(null);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);

  const execute = async (op: "activate" | "pause" | "resume" | "stop" | "checkpoint" | "restore", label: string) => {
    if (confirmAction !== op) {
      setConfirmAction(op);
      return;
    }
    setConfirmAction(null);
    setFeedback({ kind: "processing", msg: `Processing ${label.toLowerCase()}…` });
    const res = await paperApi[op](deploymentId);
    if (res.ok) {
      setFeedback({ kind: "success", msg: `${label} succeeded.` });
      onRefresh();
    } else {
      setFeedback({ kind: "error", msg: `Failed: ${res.error.message}` });
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  const resetCb = async () => {
    if (confirmAction !== "reset_cb") {
      setConfirmAction("reset_cb");
      return;
    }
    setConfirmAction(null);
    setFeedback({ kind: "processing", msg: "Resetting circuit breaker…" });
    const res = await paperApi.resetCircuitBreaker(deploymentId);
    if (res.ok) {
      setFeedback({ kind: "success", msg: "Circuit breaker reset." });
      onRefresh();
    } else {
      setFeedback({ kind: "error", msg: `Failed: ${res.error.message}` });
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  const isFailed = status === "failed";
  const isStopped = status === "stopped";
  const isPaused = status === "paused";
  const isActive = status === "active";

  return (
    <Panel title="Lifecycle Controls">
      <div className="stack gap-sm">
        {feedback && <Feedback kind={feedback.kind}>{feedback.msg}</Feedback>}
        <div className="controls-group">
          <span className="cg-label">Deployment</span>
          <div className="row gap-sm wrap">
            {(isActive || isPaused || isStopped) && (
              <Button variant="primary" size="sm" onClick={() => execute("activate", "Activate")} disabled={isFailed}>Activate</Button>
            )}
            {isActive && (
              <Button variant="secondary" size="sm" onClick={() => execute("pause", "Pause")} disabled={isFailed}>Pause</Button>
            )}
            {isPaused && (
              <Button variant="secondary" size="sm" onClick={() => execute("resume", "Resume")} disabled={isFailed}>Resume</Button>
            )}
            {(isActive || isPaused) && (
              <Button variant="danger" size="sm" onClick={() => execute("stop", "Stop")} disabled={isFailed}>Stop</Button>
            )}
            {isFailed && <span className="faint" style={{ fontSize: 11.5 }}>Deployment is failed. No lifecycle actions available.</span>}
            {isStopped && <span className="faint" style={{ fontSize: 11.5 }}>Deployment is stopped. No lifecycle actions available.</span>}
          </div>
        </div>
        <div className="controls-group">
          <span className="cg-label">Session</span>
          <div className="row gap-sm wrap">
            <Button variant="secondary" size="sm" onClick={() => execute("checkpoint", "Checkpoint")} disabled={isFailed || isStopped}>Checkpoint</Button>
            <Button variant="warning" size="sm" onClick={() => execute("restore", "Restore")} disabled={isFailed || isStopped}>Restore</Button>
            <Button variant="danger" size="sm" onClick={resetCb} disabled={isFailed || isStopped}>Reset Circuit Breaker</Button>
          </div>
        </div>
      </div>
      <ConfirmDialog
        open={confirmAction === "stop"}
        title="Stop deployment?"
        message="This action is terminal. Stopped deployments cannot be restarted."
        confirmLabel="Stop"
        danger
        onConfirm={() => execute("stop", "Stop")}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmDialog
        open={confirmAction === "reset_cb"}
        title="Reset circuit breaker?"
        message="This will close the circuit breaker and allow trading to resume if the deployment is active."
        confirmLabel="Reset"
        onConfirm={resetCb}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmDialog
        open={confirmAction === "restore"}
        title="Restore session?"
        message="This will restore the session from the latest checkpoint, modifying runtime session state."
        confirmLabel="Restore"
        onConfirm={() => execute("restore", "Restore")}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmDialog
        open={confirmAction === "checkpoint"}
        title="Save checkpoint?"
        message="This will save the current session state as a checkpoint."
        confirmLabel="Save"
        onConfirm={() => execute("checkpoint", "Checkpoint")}
        onCancel={() => setConfirmAction(null)}
      />
    </Panel>
  );
}

function isCriticalEvent(type: string): boolean {
  return ["order_rejected", "health_warning", "circuit_breaker_tripped", "deployment_stopped"].includes(type);
}

function isWarningEvent(type: string): boolean {
  return ["risk_warning", "order_rejected"].includes(type);
}
