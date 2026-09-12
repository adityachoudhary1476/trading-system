"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { fmt, fmtPct } from "@/lib/format";
import { type WidgetInstance } from "@/store/terminal";
import { DashboardSnapshotResponse, DeploymentListResponse } from "@/types/paper-api";

export function PaperPnlWidget({ _widget }: { _widget: WidgetInstance }) {
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

  const { data: dashboardResult } = useQuery({
    queryKey: ["paper-dashboard", deploymentId],
    queryFn: () => paperApi.getDashboard(deploymentId),
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

  const isDashboardOk = (result: typeof dashboardResult): result is { ok: true; data: DashboardSnapshotResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract data safely - check inline without type predicate
  const _deployments = isDeploymentsOk(deploymentsResult) ? deploymentsResult.data.deployments : [];
  const dashboard = isDashboardOk(dashboardResult) ? dashboardResult.data : undefined;
  const bot = (botResult !== undefined && botResult.ok === true) ? botResult.data.bot : undefined;

  if (!deploymentId) {
    return <div className="p-2 dim text-center">No paper deployments found</div>;
  }

  const account = dashboard?.account;
  const perf = dashboard?.performance;

  const equity = account?.equity ?? 0;
  const starting = account?.starting_equity ?? account?.initial_cash ?? 0;
  const totalPnl = perf?.total_pnl ?? (account?.realized_pnl ?? 0) + (account?.unrealized_pnl ?? 0);
  const returnPct = starting > 0 ? totalPnl / starting : 0;
  const drawdown = perf?.drawdown ?? 0;
  const trades = perf?.trade_count ?? 0;
  const winRate = perf?.win_rate ?? 0;
  const profitFactor = perf?.profit_factor ?? 0;

  const botDecisionCount = bot?.decision_count ?? 0;
  const botDeployments = bot?.deployment_count ?? 0;
  const botEvents = bot?.event_count ?? 0;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">PAPER P&L</span>
        <span className="pill text-[10px]">LIVE</span>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <div className="dim">EQUITY</div>
          <div className="font-bold text-[16px] font-mono text-[var(--text)]">₹{fmt(equity)}</div>
        </div>
        <div>
          <div className="dim">TOTAL P&L</div>
          <div className={`font-bold text-[16px] font-mono ${totalPnl >= 0 ? "up" : "down"}`}>
            {totalPnl >= 0 ? "+" : ""}₹{fmt(Math.abs(totalPnl))}
          </div>
        </div>
        <div>
          <div className="dim">RETURN</div>
          <div className={`font-bold text-[16px] font-mono ${returnPct >= 0 ? "up" : "down"}`}>
            {returnPct >= 0 ? "+" : ""}{fmtPct(returnPct)}
          </div>
        </div>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <div className="dim">DRAWDOWN</div>
          <div className="font-bold text-[13px] font-mono down">{fmtPct(drawdown)}</div>
        </div>
        <div>
          <div className="dim">TRADES</div>
          <div className="font-bold text-[13px] font-mono">{trades}</div>
        </div>
        <div>
          <div className="dim">WIN RATE</div>
          <div className="font-bold text-[13px] font-mono">{Math.round(winRate * 100)}%</div>
        </div>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <div className="dim">PROFIT FACTOR</div>
          <div className="font-bold text-[13px] font-mono">{profitFactor.toFixed(2)}</div>
        </div>
        <div>
          <div className="dim">STARTING</div>
          <div className="font-bold text-[13px] font-mono">₹{fmt(starting)}</div>
        </div>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)]">
        <div className="dim text-[9px] uppercase tracking-wider mb-1">AUTONOMOUS BOT</div>
        <div className="grid grid-cols-3 gap-2 text-[11px]">
          <div>
            <div className="dim">Decisions</div>
            <div className="font-mono">{botDecisionCount}</div>
          </div>
          <div>
            <div className="dim">Deployments</div>
            <div className="font-mono">{botDeployments}</div>
          </div>
          <div>
            <div className="dim">Events</div>
            <div className="font-mono">{botEvents}</div>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-2">
        <div className="dim text-[9px] uppercase tracking-wider mb-2">ACCOUNT DETAIL</div>
        <table className="data-table w-full" style={{ fontSize: 11 }}>
          <tbody>
            <tr className="border-b border-[var(--border-soft)]">
              <td className="p-1 dim">Initial Cash</td>
              <td className="p-1 text-right font-mono">₹{fmt(account?.initial_cash ?? 0)}</td>
            </tr>
            <tr className="border-b border-[var(--border-soft)]">
              <td className="p-1 dim">Available Cash</td>
              <td className="p-1 text-right font-mono">₹{fmt(account?.available_cash ?? 0)}</td>
            </tr>
            <tr className="border-b border-[var(--border-soft)]">
              <td className="p-1 dim">Realized P&L</td>
              <td className={`p-1 text-right font-mono ${account?.realized_pnl !== undefined && account.realized_pnl >= 0 ? "up" : "down"}`}>
                {account?.realized_pnl !== undefined ? (account.realized_pnl >= 0 ? "+" : "") + "₹" + fmt(Math.abs(account.realized_pnl)) : "—"}
              </td>
            </tr>
            <tr className="border-b border-[var(--border-soft)]">
              <td className="p-1 dim">Unrealized P&L</td>
              <td className={`p-1 text-right font-mono ${account?.unrealized_pnl !== undefined && account.unrealized_pnl >= 0 ? "up" : "down"}`}>
                {account?.unrealized_pnl !== undefined ? (account.unrealized_pnl >= 0 ? "+" : "") + "₹" + fmt(Math.abs(account.unrealized_pnl)) : "—"}
              </td>
            </tr>
            <tr className="border-b border-[var(--border-soft)]">
              <td className="p-1 dim">Margin Used</td>
              <td className="p-1 text-right font-mono">₹{fmt(account?.margin_used ?? 0)}</td>
            </tr>
            <tr>
              <td className="p-1 dim">Orders Submitted</td>
              <td className="p-1 text-right font-mono">{perf?.orders_submitted ?? 0}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        Paper trading only · No real money at risk
      </div>
    </div>
  );
}