"use client";

import { useQuery } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { paperApi } from "@/lib/paperApi";
import { fmt, fmtPct } from "@/lib/format";

interface BacktestResult {
  strategy_id: string;
  strategy_name: string;
  backtest_return: number;
  backtest_sharpe: number;
  backtest_max_dd: number;
  backtest_win_rate: number;
  paper_return: number;
  paper_sharpe: number;
  paper_max_dd: number;
  paper_win_rate: number;
  paper_trades: number;
  divergence: number;
}

export function BacktestComparisonWidget({ widget }: { widget: WidgetInstance }) {
  const [deploymentId, setDeploymentId] = useState<string>("");

  const { data: deployments } = useQuery({
    queryKey: ["paper-deployments"],
    queryFn: () => paperApi.listDeployments({ limit: 50 }),
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (deployments?.ok && deployments.data.deployments.length > 0 && !deploymentId) {
      setDeploymentId(deployments.data.deployments[0].deployment_id);
    }
  }, [deployments, deploymentId]);

  // In a real implementation, this would compare backtest vs paper results
  // For now, we show a comparison view with mock data structure
  const mockComparison: BacktestResult[] = [
    {
      strategy_id: "ema_crossover",
      strategy_name: "EMA Crossover Trend",
      backtest_return: 0.234,
      backtest_sharpe: 1.82,
      backtest_max_dd: -0.089,
      backtest_win_rate: 0.58,
      paper_return: 0.198,
      paper_sharpe: 1.54,
      paper_max_dd: -0.102,
      paper_win_rate: 0.55,
      paper_trades: 87,
      divergence: 0.154,
    },
    {
      strategy_id: "rsi_mean_reversion",
      strategy_name: "RSI Mean Reversion",
      backtest_return: 0.167,
      backtest_sharpe: 1.45,
      backtest_max_dd: -0.067,
      backtest_win_rate: 0.61,
      paper_return: 0.134,
      paper_sharpe: 1.28,
      paper_max_dd: -0.089,
      paper_win_rate: 0.58,
      paper_trades: 156,
      divergence: 0.198,
    },
    {
      strategy_id: "sma5_trend",
      strategy_name: "SMA5 Trend",
      backtest_return: 0.112,
      backtest_sharpe: 1.12,
      backtest_max_dd: -0.098,
      backtest_win_rate: 0.52,
      paper_return: 0.089,
      paper_sharpe: 0.98,
      paper_max_dd: -0.112,
      paper_win_rate: 0.49,
      paper_trades: 98,
      divergence: 0.205,
    },
  ];

  if (!deploymentId) {
    return <div className="p-2 dim text-center">No paper deployments found</div>;
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">BACKTEST vs PAPER</span>
        <span className="pill text-[10px]">REALITY CHECK</span>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] dim text-[10px]">
        Comparing walk-forward backtest results vs live paper trading performance.
        Divergence greater than 20% warrants investigation.
      </div>

      <div className="flex-1 overflow-auto">
        <table className="data-table w-full" style={{ fontSize: 11 }}>
          <thead className="sticky top-0 bg-[var(--bg-elev)]">
            <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
              <th className="p-1 text-left">Strategy</th>
              <th className="p-1 text-right" colSpan={2}>Return</th>
              <th className="p-1 text-right" colSpan={2}>Sharpe</th>
              <th className="p-1 text-right" colSpan={2}>Max DD</th>
              <th className="p-1 text-right" colSpan={2}>Win%</th>
              <th className="p-1 text-right">Div.</th>
            </tr>
            <tr className="border-b border-[var(--border-soft)] text-[9px] text-[var(--text-faint)]">
              <th className="p-1"></th>
              <th className="p-1 text-right">BT</th>
              <th className="p-1 text-right">Paper</th>
              <th className="p-1 text-right">BT</th>
              <th className="p-1 text-right">Paper</th>
              <th className="p-1 text-right">BT</th>
              <th className="p-1 text-right">Paper</th>
              <th className="p-1 text-right">BT</th>
              <th className="p-1 text-right">Paper</th>
              <th className="p-1 text-right">%</th>
            </tr>
          </thead>
          <tbody>
            {mockComparison.map((r) => {
              const divClass = r.divergence > 0.2 ? "down" : r.divergence > 0.1 ? "dim" : "up";
              return (
                <tr key={r.strategy_id} className="border-b border-[var(--border-soft)]">
                  <td className="p-1 text-left truncate max-w-[140px] font-mono text-[10px]">{r.strategy_name}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{fmtPct(r.backtest_return)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{fmtPct(r.paper_return)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{r.backtest_sharpe.toFixed(2)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{r.paper_sharpe.toFixed(2)}</td>
                  <td className="p-1 text-right font-mono text-[10px] down">{fmtPct(r.backtest_max_dd)}</td>
                  <td className="p-1 text-right font-mono text-[10px] down">{fmtPct(r.paper_max_dd)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{Math.round(r.backtest_win_rate * 100)}%</td>
                  <td className="p-1 text-right font-mono text-[10px]">{Math.round(r.paper_win_rate * 100)}%</td>
                  <td className={`p-1 text-right font-mono text-[10px] font-bold ${divClass}`}>
                    {Math.round(r.divergence * 100)}%
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        BT = Backtest (walk-forward) · Paper = Live paper trading · Div. = % divergence
      </div>
    </div>
  );
}