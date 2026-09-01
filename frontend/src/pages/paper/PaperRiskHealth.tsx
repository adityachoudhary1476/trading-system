import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { HealthResponse, RiskResponse, CircuitBreakerResponse } from "@/types/paper-api";
import { Panel, EmptyState, Loading, Button, StatusIndicator, ConfirmDialog } from "@/components/ui";
import { DeploymentPicker } from "@/components/paper/paperShared";

export function PaperRiskHealth() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [risk, setRisk] = useState<RiskResponse | null>(null);
  const [cb, setCb] = useState<CircuitBreakerResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  const fetch = () => {
    if (!deploymentIdState) return;
    setLoading(true);
    setError(null);
    Promise.all([
      paperApi.getHealth(deploymentIdState),
      paperApi.getRisk(deploymentIdState),
      paperApi.getCircuitBreaker(deploymentIdState),
    ]).then(([h, r, c]) => {
      if (h.ok) setHealth(h.data);
      if (r.ok) setRisk(r.data);
      if (c.ok) setCb(c.data);
      if (!h.ok || !r.ok || !c.ok) setError("Failed to load risk/health data");
      setLoading(false);
    });
  };

  useEffect(() => {
    fetch();
  }, [deploymentIdState]);

  const resetCircuitBreaker = async () => {
    setShowResetConfirm(false);
    setFeedback("Resetting circuit breaker…");
    const res = await paperApi.resetCircuitBreaker(deploymentIdState);
    if (res.ok) {
      setFeedback("Circuit breaker reset.");
      fetch();
    } else {
      setFeedback(`Failed: ${res.error.message}`);
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Select Deployment</h2></div>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view risk & health…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its health, risk, and circuit breaker status."
        />
      </div>
    );
  }

  return (
    <div className="paper-shell">
      {loading && <Loading label="Loading risk & health…" />}

      {error && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load risk & health</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={fetch}>Retry</Button>
        </div>
      )}

      {!loading && !error && (
        <>
          {feedback && (
            <div className={`feedback ${feedback.includes("Failed") ? "fb-error" : feedback.includes("reset") && !feedback.includes("Failed") ? "fb-success" : "fb-processing"}`} role="status">
              {feedback}
            </div>
          )}

          {/* Health */}
          <div className="pt-section"><h2>Health</h2></div>
          {health && (
            <div className={`risk-block ${health.health.status === "healthy" ? "rb-healthy" : health.health.status === "warning" ? "rb-warning" : "rb-critical"}`}>
              <div className="rb-top">
                <StatusIndicator status={health.health.status} />
              </div>
              {health.health.halt_reason && <div className="rb-reason">{health.health.halt_reason}</div>}
              {health.health.warnings.length > 0 && (
                <div className="rb-detail">
                  <ul className="tag-list">
                    {health.health.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                </div>
              )}
              {health.health.warnings.length === 0 && !health.health.halt_reason && (
                <div className="faint" style={{ fontSize: 11.5 }}>All systems nominal</div>
              )}
            </div>
          )}

          {/* Risk */}
          <div className="pt-section"><h2>Risk</h2></div>
          {risk && (
            <div className={`risk-block ${risk.risk.decision === "allow" ? "rb-healthy" : risk.risk.decision === "warning" ? "rb-warning" : "rb-critical"}`}>
              <div className="rb-top">
                <StatusIndicator status={risk.risk.decision === "allow" ? "active" : risk.risk.decision === "warning" ? "paused" : "stopped"} />
                <span style={{ fontSize: 12, color: "var(--text)", fontWeight: 600, marginLeft: 4 }}>{risk.risk.decision.toUpperCase()}</span>
              </div>
              {risk.risk.reason && <div className="rb-reason">{risk.risk.reason}</div>}
              {risk.risk.decision === "allow" && !risk.risk.reason && (
                <div className="faint" style={{ fontSize: 11.5 }}>Trading allowed — within risk limits</div>
              )}
            </div>
          )}

          {/* Circuit Breaker */}
          <div className="pt-section">
            <h2>Circuit Breaker</h2>
            {cb?.circuit_breaker.state === "open" && (
              <Button variant="danger" size="sm" onClick={() => setShowResetConfirm(true)}>Reset Circuit Breaker</Button>
            )}
          </div>
          {cb && (
            <div className={`risk-block ${cb.circuit_breaker.state === "open" ? "rb-critical circuit-open" : "rb-healthy"}`}>
              <div className="rb-top">
                <StatusIndicator status={cb.circuit_breaker.state === "open" ? "open" : "closed"} />
              </div>
              {cb.circuit_breaker.reason && <div className="rb-reason">{cb.circuit_breaker.reason}</div>}
              <div className="rb-detail">
                <span className="pill">Trip count: {cb.circuit_breaker.trip_count}</span>
              </div>
              {cb.circuit_breaker.state === "open" && (
                <div className="mt-md">
                  <div className="faint" style={{ fontSize: 11.5, color: "var(--negative)" }}>
                    Trading halted — circuit breaker is OPEN. Reset to resume.
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Navigation */}
          <Panel title="Navigate" className="mt-md">
            <div className="controls-group">
              <Link to={`/paper/deployments/${deploymentIdState}`} className="btn btn-secondary btn-sm">Deployment Detail</Link>
              <Link to={`/paper/sessions/${deploymentIdState}`} className="btn btn-secondary btn-sm">Session</Link>
              <Link to={`/paper/events/${deploymentIdState}`} className="btn btn-secondary btn-sm">Events</Link>
              <Link to={`/paper/reports/${deploymentIdState}`} className="btn btn-secondary btn-sm">Reports</Link>
            </div>
          </Panel>
        </>
      )}

      <ConfirmDialog
        open={showResetConfirm}
        title="Reset circuit breaker?"
        message="This will close the circuit breaker and allow trading to resume if the deployment is active."
        confirmLabel="Reset"
        danger
        onConfirm={resetCircuitBreaker}
        onCancel={() => setShowResetConfirm(false)}
      />
    </div>
  );
}
