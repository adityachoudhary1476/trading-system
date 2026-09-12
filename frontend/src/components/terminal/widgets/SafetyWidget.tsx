"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { type WidgetInstance } from "@/store/terminal";
import { HealthResponse, RiskResponse, CircuitBreakerResponse, DeploymentListResponse } from "@/types/paper-api";

export function SafetyWidget({ _widget }: { _widget: WidgetInstance }) {
  const [deploymentId, setDeploymentId] = useState<string>("");

  const { data: deploymentsResult } = useQuery({
    queryKey: ["paper-deployments"],
    queryFn: () => paperApi.listDeployments({ limit: 50 }),
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (deploymentsResult?.ok && deploymentsResult.data.deployments.length > 0 && !deploymentId) {
      setDeploymentId(deploymentsResult.data.deployments[0].deployment_id);
    }
  }, [deploymentsResult, deploymentId]);

  const { data: healthResult } = useQuery({
    queryKey: ["paper-health", deploymentId],
    queryFn: () => paperApi.getHealth(deploymentId),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  const { data: riskResult } = useQuery({
    queryKey: ["paper-risk", deploymentId],
    queryFn: () => paperApi.getRisk(deploymentId),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  const { data: cbResult } = useQuery({
    queryKey: ["paper-circuit-breaker", deploymentId],
    queryFn: () => paperApi.getCircuitBreaker(deploymentId),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  const { data: botResult } = useQuery({
    queryKey: ["autonomous-bot"],
    queryFn: () => paperApi.getAutonomousBot(),
    refetchInterval: 10_000,
  });

  // Type guards
  const isDeploymentsOk = (result: typeof deploymentsResult): result is { ok: true; data: DeploymentListResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isHealthOk = (result: typeof healthResult): result is { ok: true; data: HealthResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isRiskOk = (result: typeof riskResult): result is { ok: true; data: RiskResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isCbOk = (result: typeof cbResult): result is { ok: true; data: CircuitBreakerResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract data safely - check inline without type predicate
  const _deployments = isDeploymentsOk(deploymentsResult) ? deploymentsResult.data.deployments : [];
  const health = isHealthOk(healthResult) ? healthResult.data.health : undefined;
  const risk = isRiskOk(riskResult) ? riskResult.data.risk : undefined;
  const cb = isCbOk(cbResult) ? cbResult.data.circuit_breaker : undefined;
  const killSwitch = (botResult !== undefined && botResult.ok === true) ? botResult.data.bot.safety : undefined;

  if (!deploymentId) {
    return <div className="p-2 dim text-center">No paper deployments found</div>;
  }

  const healthStatus = health?.status ?? "unknown";
  const riskDecision = risk?.decision ?? "unknown";
  const cbState = cb?.state ?? "unknown";

  const getStatusColor = (status: string) => {
    if (status === "healthy" || status === "allow" || status === "closed") return "up";
    if (status === "warning") return "dim";
    return "down";
  };

  const handleResetCB = async () => {
    if (cbState === "open") {
      await paperApi.resetCircuitBreaker(deploymentId);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">SAFETY / KILL SWITCH</span>
        <span className={`pill ${healthStatus === "halted" ? "down" : healthStatus === "warning" ? "dim" : "up"} text-[10px}`}>
          {healthStatus.toUpperCase()}
        </span>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-3 gap-2 text-[11px]">
        <div className={`p-2 rounded ${getStatusColor(healthStatus) === "up" ? "bg-[var(--positive-soft)]" : getStatusColor(healthStatus) === "down" ? "bg-[var(--negative-soft)]" : "bg-[var(--warning-soft)]"}`}>
          <div className="dim text-[9px] uppercase">HEALTH</div>
          <div className="font-bold text-[13px] uppercase" style={{ color: getStatusColor(healthStatus) === "up" ? "var(--positive)" : getStatusColor(healthStatus) === "down" ? "var(--negative)" : "var(--warning)" }}>
            {healthStatus}
          </div>
          {health?.halt_reason && <div className="text-[10px] dim mt-1">{health.halt_reason}</div>}
        </div>
        <div className={`p-2 rounded ${getStatusColor(riskDecision) === "up" ? "bg-[var(--positive-soft)]" : getStatusColor(riskDecision) === "down" ? "bg-[var(--negative-soft)]" : "bg-[var(--warning-soft)]"}`}>
          <div className="dim text-[9px] uppercase">RISK ENGINE</div>
          <div className="font-bold text-[13px] uppercase" style={{ color: getStatusColor(riskDecision) === "up" ? "var(--positive)" : getStatusColor(riskDecision) === "down" ? "var(--negative)" : "var(--warning)" }}>
            {riskDecision}
          </div>
          {risk?.reason && <div className="text-[10px] dim mt-1">{risk.reason}</div>}
        </div>
        <div className={`p-2 rounded ${getStatusColor(cbState) === "up" ? "bg-[var(--positive-soft)]" : "bg-[var(--negative-soft)]"}`}>
          <div className="dim text-[9px] uppercase">CIRCUIT BREAKER</div>
          <div className="font-bold text-[13px] uppercase" style={{ color: getStatusColor(cbState) === "up" ? "var(--positive)" : "var(--negative)" }}>
            {cbState}
          </div>
          {cb?.trip_count !== undefined && <div className="text-[10px] dim mt-1">Trips: {cb.trip_count}</div>}
        </div>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)]">
        <div className="dim text-[9px] uppercase tracking-wider mb-2">KILL SWITCH</div>
        <div className="flex items-center justify-between">
          <div>
            <div className="font-mono text-[11px]">State: <span className={`ml-2 ${killSwitch?.kill_switch_state === "active" ? "down" : "up"}`}>{killSwitch?.kill_switch_state ?? "UNKNOWN"}</span></div>
            {killSwitch?.kill_switch_reason && (
              <div className="text-[10px] dim mt-1">Reason: {killSwitch.kill_switch_reason}</div>
            )}
            {killSwitch?.kill_switch_halted_at && (
              <div className="text-[10px] dim mt-1">Halted: {killSwitch.kill_switch_halted_at.slice(0, 19)}</div>
            )}
          </div>
          {killSwitch?.kill_switch_state === "active" && (
            <button
              onClick={async () => { /* resume action */ }}
              className="btn btn-primary btn-sm"
            >
              Resume (Manual)
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-auto p-2">
        <div className="dim text-[9px] uppercase tracking-wider mb-2">HEALTH WARNINGS</div>
        {health?.warnings && health.warnings.length > 0 ? (
          <div className="space-y-1">
            {health.warnings.map((w, i) => (
              <div key={i} className="p-2 text-[11px] bg-[var(--warning-soft)] border border-[var(--warning)] rounded dim">
                ⚠ {w}
              </div>
            ))}
          </div>
        ) : (
          <div className="p-4 text-center dim text-[11px]">No health warnings</div>
        )}
      </div>

      {cbState === "open" && (
        <div className="p-2 border-t border-[var(--border)]">
          <button onClick={handleResetCB} className="btn btn-warning btn-sm w-full">
            Reset Circuit Breaker
          </button>
        </div>
      )}

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        Paper-only mode · No real-money execution possible
      </div>
    </div>
  );
}