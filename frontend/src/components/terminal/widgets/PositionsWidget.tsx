"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { fmt, fmtPct } from "@/lib/format";
import { type WidgetInstance } from "@/store/terminal";
import { PositionsResponse, AccountResponse, DeploymentListResponse } from "@/types/paper-api";

export function PositionsWidget({ _widget }: { _widget: WidgetInstance }) {
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

  const { data: positionsResult, isLoading } = useQuery({
    queryKey: ["paper-positions", deploymentId],
    queryFn: () => paperApi.getPositions(deploymentId),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  const { data: accountResult } = useQuery({
    queryKey: ["paper-account", deploymentId],
    queryFn: () => paperApi.getAccount(deploymentId),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  // Type guards
  const isDeploymentsOk = (result: typeof deploymentsResult): result is { ok: true; data: DeploymentListResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isPositionsOk = (result: typeof positionsResult): result is { ok: true; data: PositionsResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isAccountOk = (result: typeof accountResult): result is { ok: true; data: AccountResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract data safely
  const deployments = isDeploymentsOk(deploymentsResult) ? deploymentsResult.data.deployments : [];
  const positions = isPositionsOk(positionsResult) ? positionsResult.data.positions : undefined;
  const account = isAccountOk(accountResult) ? accountResult.data.account : undefined;

  if (!deploymentId) {
    return (
      <div className="p-2 dim text-center">
        No paper deployments found. Create one from the Paper Trading page.
      </div>
    );
  }

  const openPos = positions?.open_position;
  const isFlat = positions?.is_flat ?? true;
  const optionsPos = positions?.options_position;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">POSITIONS</span>
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

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <div className="dim">Equity</div>
          <div className="font-mono text-[var(--text)]">{account?.equity ? `₹${fmt(account.equity)}` : "—"}</div>
        </div>
        <div>
          <div className="dim">Cash</div>
          <div className="font-mono text-[var(--text)]">{account?.available_cash ? `₹${fmt(account.available_cash)}` : "—"}</div>
        </div>
        <div>
          <div className="dim">Unrealized P&L</div>
          <div className={`font-mono ${account?.unrealized_pnl && account.unrealized_pnl >= 0 ? "up" : "down"}`}>
            {account?.unrealized_pnl !== undefined ? `₹${fmt(account.unrealized_pnl)}` : "—"}
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-center dim">Loading positions…</div>
        ) : isFlat || !openPos ? (
          <div className="p-4 text-center dim">
            <div className="text-[var(--text-dim)] mb-1">No open equity positions</div>
            <div className="text-[11px]">Flat</div>
          </div>
        ) : (
          <table className="data-table w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 bg-[var(--bg-elev)]">
              <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                <th className="p-1 text-left">Symbol</th>
                <th className="p-1 text-right">Side</th>
                <th className="p-1 text-right">Qty</th>
                <th className="p-1 text-right">Avg Entry</th>
                <th className="p-1 text-right">LTP</th>
                <th className="p-1 text-right">Mkt Value</th>
                <th className="p-1 text-right">Unrealized</th>
                <th className="p-1 text-right">Return</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-[var(--border-soft)]">
                <td className="p-1 font-mono">{openPos.symbol}</td>
                <td className="p-1 text-right">
                  <span className={`pill ${openPos.side === "long" ? "pos" : "neg"}`}>
                    {openPos.side.toUpperCase()}
                  </span>
                </td>
                <td className="p-1 text-right font-mono">
                  {Number.isFinite(openPos.quantity) ? openPos.quantity.toLocaleString("en-IN") : "—"}
                </td>
                <td className="p-1 text-right font-mono">{fmt(openPos.entry_price)}</td>
                <td className="p-1 text-right font-mono">{fmt(openPos.current_price)}</td>
                <td className="p-1 text-right font-mono">{fmt(openPos.position_value)}</td>
                <td className={`p-1 text-right font-mono ${openPos.unrealized_pnl && openPos.unrealized_pnl >= 0 ? "up" : "down"}`}>
                  {fmt(openPos.unrealized_pnl)}
                </td>
                <td className={`p-1 text-right font-mono ${openPos.unrealized_pnl && openPos.unrealized_pnl >= 0 ? "up" : "down"}`}>
                  {openPos.unrealized_pnl !== null && openPos.entry_price > 0
                    ? fmtPct(openPos.unrealized_pnl / openPos.position_value)
                    : "—"}
                </td>
              </tr>
            </tbody>
          </table>
        )}

        {optionsPos && (
          <div className="mt-4 p-2 border-t border-[var(--border-soft)]">
            <div className="dim text-[10px] uppercase tracking-wider mb-2">OPTIONS POSITION</div>
            <table className="data-table w-full" style={{ fontSize: 11 }}>
              <thead className="sticky top-0 bg-[var(--bg-elev)]">
                <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                  <th className="p-1 text-left">Contract</th>
                  <th className="p-1 text-right">Side</th>
                  <th className="p-1 text-right">Qty</th>
                  <th className="p-1 text-right">Entry</th>
                  <th className="p-1 text-right">Current</th>
                  <th className="p-1 text-right">Unrealized</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="p-1 font-mono text-[10px]">
                    {optionsPos.symbol} {optionsPos.strike} {optionsPos.option_type} {optionsPos.expiry?.slice(0, 10)}
                  </td>
                  <td className="p-1 text-right">
                    <span className={`pill ${optionsPos.side === "long" ? "pos" : "neg"}`}>
                      {optionsPos.side.toUpperCase()}
                    </span>
                  </td>
                  <td className="p-1 text-right font-mono">{optionsPos.quantity}</td>
                  <td className="p-1 text-right font-mono">{fmt(optionsPos.avg_entry_price)}</td>
                  <td className="p-1 text-right font-mono">{fmt(optionsPos.current_price)}</td>
                  <td className={`p-1 text-right font-mono ${optionsPos.realized_pnl && optionsPos.realized_pnl >= 0 ? "up" : "down"}`}>
                    {fmt(optionsPos.realized_pnl)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}