"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { fmt, fmtPct } from "@/lib/format";
import { type WidgetInstance } from "@/store/terminal";
import { EventsResponse, DeploymentListResponse } from "@/types/paper-api";

interface Signal {
  signal_id: string;
  symbol: string;
  timeframe: string;
  action: "BUY" | "SELL" | "HOLD";
  confidence: number;
  strategy_id: string;
  signal_timestamp: string;
  indicators?: Record<string, unknown>;
}

export function SignalsWidget({ _widget }: { _widget: WidgetInstance }) {
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

  const { data: eventsResult, isLoading } = useQuery({
    queryKey: ["paper-signals", deploymentId],
    queryFn: () => paperApi.getEvents(deploymentId, { event_type: "signal_generated", limit: 100 }),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  // Type guard for events response
  const isEventsResultOk = (result: typeof eventsResult): result is { ok: true; data: EventsResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isDeploymentsOk = (result: typeof deploymentsResult): result is { ok: true; data: DeploymentListResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract data safely
  const eventsData = isEventsResultOk(eventsResult) ? eventsResult.data.events : undefined;
  const deployments = isDeploymentsOk(deploymentsResult) ? deploymentsResult.data.deployments : [];

  if (!deploymentId) {
    return <div className="p-2 dim text-center">No paper deployments found</div>;
  }

  const signals = eventsData?.recent
    ?.filter((e) => e.event_type === "signal_generated")
    .map((e) => e.payload as unknown as Signal)
    .filter(Boolean) ?? [];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">SIGNALS</span>
        <select
          value={deploymentId}
          onChange={(e) => setDeploymentId(e.target.value)}
          className="bg-[var(--bg-elev-2)] border border-[var(--border)] rounded px-2 py-1 text-[var(--text)] text-[11px] max-w-[160px]"
        >
          {deployments.map((d) => (
            <option key={d.deployment_id} value={d.deployment_id}>
              {d.deployment_id.slice(0, 12)}… ({d.symbol})
            </option>
          ))}
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-center dim">Loading signals…</div>
        ) : signals.length === 0 ? (
          <div className="p-4 text-center dim">No signals generated yet</div>
        ) : (
          <table className="data-table w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 bg-[var(--bg-elev)]">
              <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                <th className="p-1 text-left">Time</th>
                <th className="p-1 text-left">Symbol</th>
                <th className="p-1 text-right">Action</th>
                <th className="p-1 text-right">Confidence</th>
                <th className="p-1 text-left">Strategy</th>
                <th className="p-1 text-right">Timeframe</th>
              </tr>
            </thead>
            <tbody>
              {[...signals].reverse().slice(0, 50).map((signal, i) => {
                const actionClass = signal.action === "BUY" ? "up" : signal.action === "SELL" ? "down" : "dim";
                const confPct = Math.round(signal.confidence * 100);
                return (
                  <tr key={signal.signal_id ?? i} className="border-b border-[var(--border-soft)]">
                    <td className="p-1 font-mono text-[10px]">{signal.signal_timestamp?.slice(11, 19) ?? "—"}</td>
                    <td className="p-1 font-mono text-[10px]">{signal.symbol}</td>
                    <td className="p-1 text-right">
                      <span className={`pill ${actionClass} text-[10px]`}>{signal.action}</span>
                    </td>
                    <td className="p-1 text-right font-mono text-[10px]">
                      <span className={confPct >= 80 ? "up" : confPct >= 60 ? "dim" : "down"}>
                        {confPct}%
                      </span>
                    </td>
                    <td className="p-1 text-left font-mono text-[10px] truncate max-w-[120px]">{signal.strategy_id}</td>
                    <td className="p-1 text-right font-mono text-[10px]">{signal.timeframe}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        {signals.length} signals total · Latest: {signals[0]?.signal_timestamp?.slice(11, 19) ?? "—"}
      </div>
    </div>
  );
}