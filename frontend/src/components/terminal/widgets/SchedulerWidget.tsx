"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { type WidgetInstance } from "@/store/terminal";
import { DeploymentListResponse } from "@/types/paper-api";

interface SchedulerDeployment {
  deployment_id: string | null;
  liveness: string;
  last_tick_at: string | null;
  last_successful_tick_at: string | null;
  last_market_data_at: string | null;
  last_decision_at: string | null;
  last_execution_at: string | null;
}

interface SchedulerStatus {
  worker_required: boolean;
  worker_alive: boolean;
  last_tick_at: string | null;
  last_successful_tick_at: string | null;
  deployments: SchedulerDeployment[];
}

export function SchedulerWidget({ _widget }: { _widget: WidgetInstance }) {
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

  const { data: botResult } = useQuery({
    queryKey: ["autonomous-bot"],
    queryFn: () => paperApi.getAutonomousBot(),
    refetchInterval: 10_000,
  });

  const scheduler = (botResult !== undefined && botResult.ok === true) ? botResult.data.bot.scheduler as SchedulerStatus | undefined : undefined;

  const getLivenessColor = (liveness: string) => {
    switch (liveness) {
      case "worker_alive": return "up";
      case "market_closed": return "dim";
      case "data_stale": return "down";
      case "worker_stale": return "down";
      case "worker_error": return "down";
      case "disabled": return "dim";
      default: return "dim";
    }
  };

  const getLivenessLabel = (liveness: string) => {
    const labels: Record<string, string> = {
      worker_alive: "ALIVE",
      worker_stale: "STALE",
      market_closed: "MKT CLOSED",
      data_stale: "DATA STALE",
      worker_error: "ERROR",
      disabled: "DISABLED",
    };
    return labels[liveness] || liveness;
  };

  if (!scheduler) {
    return (
      <div className="p-2 dim text-center">
        No scheduler data available. Autonomous bot may not be configured.
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">SCHEDULER</span>
        <span className={`pill ${scheduler.worker_alive ? "pos" : "neg"} text-[10px]`}>
          {scheduler.worker_alive ? "WORKER ALIVE" : "WORKER DOWN"}
        </span>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <div className="dim">Worker Required</div>
          <div className="font-mono">{scheduler.worker_required ? "YES" : "NO"}</div>
        </div>
        <div>
          <div className="dim">Worker Alive</div>
          <div className={`font-mono ${scheduler.worker_alive ? "up" : "down"}`}>
            {scheduler.worker_alive ? "YES" : "NO"}
          </div>
        </div>
        <div>
          <div className="dim">Last Tick</div>
          <div className="font-mono text-[10px]">{scheduler.last_tick_at?.slice(11, 19) ?? "—"}</div>
        </div>
        <div>
          <div className="dim">Last Success</div>
          <div className="font-mono text-[10px]">{scheduler.last_successful_tick_at?.slice(11, 19) ?? "—"}</div>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {scheduler.deployments.length === 0 ? (
          <div className="p-4 text-center dim">No active deployments monitored by scheduler</div>
        ) : (
          <table className="data-table w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 bg-[var(--bg-elev)]">
              <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                <th className="p-1 text-left">Deployment</th>
                <th className="p-1 text-right">Liveness</th>
                <th className="p-1 text-right">Last Tick</th>
                <th className="p-1 text-right">Last Data</th>
                <th className="p-1 text-right">Last Decision</th>
                <th className="p-1 text-right">Last Exec</th>
              </tr>
            </thead>
            <tbody>
              {scheduler.deployments.map((d, i) => (
                <tr key={d.deployment_id ?? i} className="border-b border-[var(--border-soft)]">
                  <td className="p-1 font-mono text-[10px] truncate max-w-[100px]">
                    {d.deployment_id?.slice(0, 12) ?? "—"}
                  </td>
                  <td className="p-1 text-right">
                    <span className={`pill ${getLivenessColor(d.liveness)} text-[9px]`}>
                      {getLivenessLabel(d.liveness)}
                    </span>
                  </td>
                  <td className="p-1 text-right font-mono text-[10px]">{d.last_tick_at?.slice(11, 19) ?? "—"}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{d.last_market_data_at?.slice(11, 19) ?? "—"}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{d.last_decision_at?.slice(11, 19) ?? "—"}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{d.last_execution_at?.slice(11, 19) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        Last tick: {scheduler.last_tick_at?.slice(0, 19) ?? "—"} · Worker: {scheduler.worker_alive ? "●" : "○"}
      </div>
    </div>
  );
}