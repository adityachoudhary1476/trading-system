"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { fmt } from "@/lib/format";
import { type WidgetInstance } from "@/store/terminal";
import { EventsResponse, DeploymentListResponse } from "@/types/paper-api";

type OrderType = "MARKET" | "LIMIT";
type OrderSide = "BUY" | "SELL";
type OrderStatus = "PENDING" | "FILLED" | "PARTIAL" | "REJECTED" | "CANCELLED";

interface OrderPayload {
  side?: OrderSide;
  status?: OrderStatus;
  order_type?: OrderType;
  quantity?: number;
  limit_price?: number;
  symbol?: string;
  filled_quantity?: number;
}

export function OrdersWidget({ _widget }: { _widget: WidgetInstance }) {
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
    queryKey: ["paper-orders", deploymentId],
    queryFn: async () => {
      const res = await paperApi.getEvents(deploymentId, { limit: 100, event_type: "order_submitted" });
      return res;
    },
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  // Type guard for ApiResult.ok === true
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

  const events = eventsData?.recent ?? [];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">ORDERS</span>
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
          <div className="p-4 text-center dim">Loading orders…</div>
        ) : events.length ? (
          <table className="data-table w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 bg-[var(--bg-elev)]">
              <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                <th className="p-1 text-left">Time</th>
                <th className="p-1 text-left">Symbol</th>
                <th className="p-1 text-right">Side</th>
                <th className="p-1 text-right">Type</th>
                <th className="p-1 text-right">Qty</th>
                <th className="p-1 text-right">Price</th>
                <th className="p-1 text-right">Status</th>
                <th className="p-1 text-right">Filled</th>
              </tr>
            </thead>
            <tbody>
              {events
                .filter((e) => e.event_type === "order_submitted" || e.event_type === "fill_received" || e.event_type === "order_rejected")
                .slice(0, 50)
                .map((event, i) => {
                  const payload = event.payload as OrderPayload | undefined;
                  const side = payload?.side || "—";
                  const status = payload?.status || (event.event_type === "order_rejected" ? "REJECTED" : "PENDING");
                  const statusClass =
                    status === "FILLED" ? "up" :
                    status === "REJECTED" ? "down" :
                    status === "CANCELLED" ? "dim" : "dim";

                  return (
                    <tr key={event.sequence ?? i} className="border-b border-[var(--border-soft)]">
                      <td className="p-1 font-mono text-[10px]">{event.timestamp?.slice(11, 19) ?? "—"}</td>
                      <td className="p-1 font-mono text-[10px]">{payload?.symbol || "—"}</td>
                      <td className="p-1 text-right">
                        <span className={`pill ${side === "BUY" ? "pos" : side === "SELL" ? "neg" : ""} text-[10px]`}>
                          {side}
                        </span>
                      </td>
                      <td className="p-1 text-right font-mono text-[10px]">{payload?.order_type || "—"}</td>
                      <td className="p-1 text-right font-mono text-[10px]">{(payload?.quantity as number)?.toLocaleString() || "—"}</td>
                      <td className="p-1 text-right font-mono text-[10px]">
                        {payload?.limit_price ? fmt(payload.limit_price) : "MKT"}
                      </td>
                      <td className="p-1 text-right">
                        <span className={`pill ${statusClass} text-[10px]`}>{status}</span>
                      </td>
                      <td className="p-1 text-right font-mono text-[10px]">
                        {(payload?.filled_quantity as number)?.toLocaleString() || "0"}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        ) : (
          <div className="p-4 text-center dim">
            <div className="text-[var(--text-dim)] mb-1">No orders found</div>
            <div className="text-[11px]">Orders will appear here as they are submitted</div>
          </div>
        )}
      </div>
    </div>
  );
}