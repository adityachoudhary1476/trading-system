import { useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { SessionResponse } from "@/types/paper-api";
import { Panel, EmptyState, Button, StatusIndicator, MetricItem, Feedback, ConfirmDialog } from "@/components/ui";
import { DeploymentPicker, fmt } from "@/components/paper/paperShared";

export function PaperSessions() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [response, setResponse] = useState<SessionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error" | "processing"; msg: string } | null>(null);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);

  const fetchSession = useCallback(async () => {
    if (!deploymentIdState) return;
    setLoading(true);
    setError(null);
    setFeedback(null);
    const res = await paperApi.getSession(deploymentIdState);
    if (res.ok) {
      setResponse(res.data);
    } else {
      setError(res.error.message);
      setResponse(null);
    }
    setLoading(false);
  }, [deploymentIdState]);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  useEffect(() => {
    fetchSession();
  }, [fetchSession]);

  const handleCheckpoint = async () => {
    if (!deploymentIdState) return;
    setConfirmAction(null);
    setFeedback({ kind: "processing", msg: "Saving checkpoint…" });
    const res = await paperApi.checkpoint(deploymentIdState);
    if (res.ok) {
      setFeedback({ kind: "success", msg: "Checkpoint saved." });
      setResponse(res.data);
    } else {
      setFeedback({ kind: "error", msg: `Failed: ${res.error.message}` });
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  const handleRestore = async () => {
    if (!deploymentIdState) return;
    setConfirmAction(null);
    setFeedback({ kind: "processing", msg: "Restoring session…" });
    const res = await paperApi.restore(deploymentIdState);
    if (res.ok) {
      setFeedback({ kind: "success", msg: "Session restored successfully." });
      setResponse(res.data);
    } else {
      setFeedback({ kind: "error", msg: `Restore failed: ${res.error.message}` });
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Paper Sessions</h2></div>
        <p className="faint" style={{ fontSize: 11.5, marginTop: -8, marginBottom: 8 }}>
          Sessions preserve paper-trading operational state and recovery checkpoints. Select a deployment to inspect its session.
        </p>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view its session…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its session state and checkpoints."
        />
      </div>
    );
  }

  const session = response?.session;
  const checkpoint = response?.checkpoint;
  const isCircuitOpen = session?.circuit_state === "open";
  const isHalted = session?.health_status === "halted";
  const isCritical = isCircuitOpen || isFailed(session);

  return (
    <div className="paper-shell">
      <div className="pt-section">
        <Link to="/paper/deployments" className="section-sub">← Paper Deployments</Link>
      </div>

      {loading && <SessionSkeleton />}

      {error && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load session</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={fetchSession}>Retry</Button>
        </div>
      )}

      {!loading && !error && response && session && (
        <>
          {feedback && (
            <Feedback kind={feedback.kind}>{feedback.msg}</Feedback>
          )}

          {/* Session Header */}
          <div className={`detail-head${isCritical ? " circuit-open" : ""}`}>
            <div className="dh-top">
              <div>
                <div className="dh-title">
                  {session.strategy_id}
                </div>
                <div className="dh-sub">
                  {session.deployment_id} · {session.symbol} · {session.timeframe}
                </div>
              </div>
              <div className="row gap-sm wrap">
                <span className="pill">{session.execution_mode.toUpperCase()}</span>
                <StatusIndicator status={session.session_status} />
                <StatusIndicator status={session.deployment_status} />
              </div>
            </div>
            <div className="dh-metrics">
              <div className="dh-metric">
                <span className="m-label">Last Bar</span>
                <span className="m-value">{session.last_processed_bar_timestamp ? new Date(session.last_processed_bar_timestamp).toLocaleString() : "—"}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Bars</span>
                <span className="m-value">{fmt.num(session.bar_count)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Orders</span>
                <span className="m-value">{fmt.num(session.orders_submitted)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Fills</span>
                <span className="m-value">{fmt.num(session.fills_received)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Events</span>
                <span className="m-value">{fmt.num(session.event_count)}</span>
              </div>
              <div className="dh-metric">
                <span className="m-label">Circuit</span>
                <span className="m-value"><StatusIndicator status={isCircuitOpen ? "open" : "closed"} /></span>
              </div>
            </div>
          </div>

          {/* Critical State Banner */}
          {isCritical && (
            <div className="risk-block rb-critical" role="alert">
              <div className="rb-top">
                <StatusIndicator status={isHalted ? "halted" : isCircuitOpen ? "open" : "failed"} />
                <span className="rb-status">
                  {isHalted ? "HALTED" : isCircuitOpen ? "CIRCUIT OPEN" : "FAILED"}
                </span>
              </div>
              <div className="rb-reason">
                {isHalted && (session.halt_reason || "Paper execution is currently blocked.")}
                {!isHalted && isCircuitOpen && (session.circuit_reason || "Circuit breaker is open.")}
                {!isHalted && !isCircuitOpen && "Session has failed."}
              </div>
            </div>
          )}

          {/* Session State + Operational Counters */}
          <div className="grid cols-2" style={{ gap: 12 }}>
            <Panel title="Session State">
              <div className="metric-grid">
                <MetricItem label="Session ID" value={session.session_id} />
                <MetricItem label="Deployment ID" value={session.deployment_id} />
                <MetricItem label="Strategy ID" value={session.strategy_id} />
                <MetricItem label="Spec Hash" value={session.strategy_spec_hash} />
                <MetricItem label="Session Status" value={session.session_status} />
                <MetricItem label="Deployment Status" value={session.deployment_status} />
                <MetricItem label="Dataset" value={session.dataset_id} />
                <MetricItem label="Created" value={session.created_at ? new Date(session.created_at).toLocaleString() : "—"} />
                <MetricItem label="Updated" value={session.updated_at ? new Date(session.updated_at).toLocaleString() : "—"} />
              </div>
            </Panel>

            <Panel title="Operational Counters">
              <div className="metric-grid">
                <MetricItem label="Last Bar" value={session.last_processed_bar_timestamp ? new Date(session.last_processed_bar_timestamp).toLocaleString() : "—"} />
                <MetricItem label="Bar Count" value={fmt.num(session.bar_count)} />
                <MetricItem label="Signals" value={fmt.num(session.generated_signals)} />
                <MetricItem label="Orders" value={fmt.num(session.orders_submitted)} />
                <MetricItem label="Fills" value={fmt.num(session.fills_received)} />
                <MetricItem label="Rejections" value={fmt.num(session.rejected_orders)} tone={session.rejected_orders > 0 ? "neg" : undefined} />
                <MetricItem label="Errors" value={fmt.num(session.consecutive_errors)} tone={session.consecutive_errors > 0 ? "neg" : undefined} />
                <MetricItem label="Event Count" value={fmt.num(session.event_count)} />
              </div>
            </Panel>
          </div>

          {/* Performance + Safety */}
          <div className="grid cols-2" style={{ gap: 12 }}>
            <Panel title="Performance">
              <div className="metric-grid">
                <MetricItem label="Starting Equity" value={fmt.currency(session.starting_equity)} />
                <MetricItem label="Current Equity" value={fmt.currency(session.current_equity)} tone={session.current_equity && session.starting_equity && session.current_equity >= session.starting_equity ? "pos" : "neg"} />
                <MetricItem label="Realized P&L" value={fmt.currency(session.realized_pnl)} tone={session.realized_pnl && session.realized_pnl >= 0 ? "pos" : "neg"} />
                <MetricItem label="Unrealized P&L" value={fmt.currency(session.unrealized_pnl)} tone={session.unrealized_pnl && session.unrealized_pnl >= 0 ? "pos" : "neg"} />
                <MetricItem label="Max Drawdown" value={fmt.currencyPlain(session.max_drawdown)} tone="neg" />
              </div>
            </Panel>

            <Panel title="Safety State" className={isCircuitOpen ? "circuit-open" : undefined}>
              <div className="metric-grid">
                <MetricItem label="Health" value={<StatusIndicator status={session.health_status} />} />
                <MetricItem label="Halt Reason" value={session.halt_reason || "—"} />
                <MetricItem label="Risk" value={<StatusIndicator status={session.circuit_state === "open" ? "open" : "closed"} />} />
                <MetricItem label="Circuit" value={<StatusIndicator status={isCircuitOpen ? "open" : "closed"} />} />
                <MetricItem label="Circuit Reason" value={session.circuit_reason || "—"} />
                <MetricItem label="Trip Count" value={String(session.circuit_trip_count)} tone={session.circuit_trip_count > 0 ? "neg" : undefined} />
              </div>
            </Panel>
          </div>

          {/* Current Checkpoint */}
          <div>
            <div className="pt-section">
              <h2>Current Checkpoint</h2>
            </div>
            {checkpoint ? (
              <Panel subtle>
                <div className="metric-grid">
                  <MetricItem label="Checkpoint ID" value={checkpoint.checkpoint_id} />
                  <MetricItem label="Session ID" value={checkpoint.session_id} />
                  <MetricItem label="Deployment ID" value={checkpoint.deployment_id} />
                  <MetricItem label="Strategy ID" value={checkpoint.strategy_id} />
                  <MetricItem label="Spec Hash" value={checkpoint.strategy_spec_hash} />
                  <MetricItem label="Dataset" value={checkpoint.dataset_id} />
                  <MetricItem label="Last Bar" value={checkpoint.last_processed_bar_timestamp ? new Date(checkpoint.last_processed_bar_timestamp).toLocaleString() : "—"} />
                  <MetricItem label="Bar Count" value={fmt.num(checkpoint.bar_count)} />
                  <MetricItem label="Schema Version" value={String(checkpoint.schema_version)} />
                  <MetricItem label="Created" value={checkpoint.created_at ? new Date(checkpoint.created_at).toLocaleString() : "—"} />
                  <MetricItem label="Session Status" value={checkpoint.session_status} />
                  <MetricItem label="Deployment Status" value={checkpoint.deployment_status} />
                </div>
              </Panel>
            ) : (
              <Panel subtle>
                <EmptyState title="No checkpoint" hint="No checkpoint has been saved for this session. Save a checkpoint to enable recovery." />
              </Panel>
            )}
          </div>

          {/* Session Actions */}
          <Panel title="Session Actions">
            <div className="controls-group">
              <Button variant="secondary" size="sm" onClick={() => setConfirmAction("checkpoint")}>Create Checkpoint</Button>
              <Button variant="warning" size="sm" onClick={() => setConfirmAction("restore")} disabled={!checkpoint}>Restore Session</Button>
            </div>
            {!checkpoint && (
              <div className="faint mt-sm" style={{ fontSize: 11.5 }}>Restore requires an existing checkpoint.</div>
            )}
          </Panel>

          {/* Navigation */}
          <Panel title="Navigate">
            <div className="controls-group">
              <Link to={`/paper/deployments/${deploymentIdState}`} className="btn btn-secondary btn-sm">Deployment Detail</Link>
              <Link to={`/paper/events/${deploymentIdState}`} className="btn btn-secondary btn-sm">Events</Link>
              <Link to={`/paper/risk/${deploymentIdState}`} className="btn btn-secondary btn-sm">Risk & Health</Link>
              <Link to={`/paper/reports/${deploymentIdState}`} className="btn btn-secondary btn-sm">Reports</Link>
            </div>
          </Panel>

          {/* Checkpoint Confirmation */}
          <ConfirmDialog
            open={confirmAction === "checkpoint"}
            title="Save checkpoint?"
            message="This will capture the current session state as a recovery checkpoint. This does not modify the running session."
            confirmLabel="Save"
            onConfirm={handleCheckpoint}
            onCancel={() => setConfirmAction(null)}
          />

          {/* Restore Confirmation */}
          <ConfirmDialog
            open={confirmAction === "restore"}
            title="Restore session?"
            message={`This will restore the session from checkpoint ${checkpoint?.checkpoint_id ?? "—"} into the current runner. Persisted state will be applied, and compatibility validation will occur. Incompatible checkpoints will be rejected. This does not switch to live trading.`}
            confirmLabel="Restore"
            onConfirm={handleRestore}
            onCancel={() => setConfirmAction(null)}
          />
        </>
      )}
    </div>
  );
}

function isFailed(session: { session_status: string; deployment_status: string } | undefined): boolean {
  if (!session) return false;
  return session.session_status === "failed" || session.deployment_status === "failed";
}

function SessionSkeleton() {
  return (
    <div className="stack gap-lg" aria-label="Loading session detail">
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
