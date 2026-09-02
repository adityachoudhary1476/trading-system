import { useEffect, useState } from "react";
import { Outlet, useParams } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { DashboardSnapshotResponse } from "@/types/paper-api";
import { Loading } from "@/components/ui";

export function PaperTradingPage() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [snapshot, setSnapshot] = useState<DashboardSnapshotResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    if (!deploymentId) {
      setSnapshot(null);
      return () => { alive = false; };
    }
    setLoading(true);
    paperApi
      .getDashboard(deploymentId)
      .then((res) => {
        if (!alive) return;
        if (res.ok) setSnapshot(res.data);
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setLoading(false);
      });
    return () => { alive = false; };
  }, [deploymentId]);

  const dep = snapshot?.deployment;
  const session = snapshot?.session;
  const health = snapshot?.health;
  const circuit = snapshot?.circuit_breaker;

  const isHalted = health?.status === "halted";
  const isCircuitOpen = circuit?.state === "open";
  const isCritical = isHalted || isCircuitOpen || dep?.status === "failed";

  const shortId = (v: string | null | undefined, take = 6) =>
    v ? v.slice(0, take).toUpperCase() : "—";

  return (
    <div className="paper-shell">
      <header className="terminal-head" role="banner">
        <div className="th-left">
          <div className="th-eyebrow">
            <span>AETHER // AUTONOMOUS TERMINAL</span>
            <span>·</span>
            <span>PAPER EXECUTION CONSOLE</span>
            {dep && (
              <>
                <span>·</span>
                <span className="th-id">DEPLOY {shortId(dep.deployment_id)}</span>
              </>
            )}
          </div>
          <div className="th-title">
            <span>Paper Trading</span>
            <span className="th-tag" title="Paper-only environment — no live broker connection">No Real Money</span>
          </div>
          <div className="th-sub">
            Autonomous simulated execution against research-validated strategies. Orders, positions, and P&amp;L are paper-only.
          </div>
        </div>
        <div className="th-right">
          <div className={`th-pulse${isCritical ? " is-critical" : ""}`}>
            <span className="th-dot" aria-hidden="true" />
            <span>{isCritical ? "Paper Halted" : "Live Paper"}</span>
          </div>
          <div className="th-meta">
            <span className="th-meta-item">
              <span className="th-meta-key">Session</span>
              <span className="th-meta-val">
                {loading && !snapshot ? "…" : shortId(session?.session_id)}
              </span>
            </span>
            <span className="th-meta-item">
              <span className="th-meta-key">Mode</span>
              <span className="th-meta-val">{(dep?.execution_mode ?? "paper").toUpperCase()}</span>
            </span>
            <span className="th-meta-item">
              <span className="th-meta-key">Status</span>
              <span className="th-meta-val">
                {(session?.session_status ?? dep?.status ?? "—").toUpperCase()}
              </span>
            </span>
          </div>
        </div>
      </header>
      {loading && !snapshot ? (
        <Loading label="Loading paper terminal…" />
      ) : (
        <Outlet />
      )}
    </div>
  );
}