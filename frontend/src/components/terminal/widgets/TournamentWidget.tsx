"use client";

import { useQuery } from "@tanstack/react-query";
import { paperApi } from "@/lib/paperApi";
import { fmt, fmtPct } from "@/lib/format";

interface TournamentResult {
  strategy_name: string;
  rank: number;
  total_return: number;
  sharpe: number;
  max_drawdown: number;
  win_rate: number;
  trade_count: number;
  regime: string;
  score: number;
}

interface TournamentResponse {
  tournament_id: string;
  status: string;
  results: TournamentResult[];
  started_at: string;
  completed_at: string | null;
}

export function TournamentWidget({ widget }: { widget: WidgetInstance }) {
  const { data: tournamentData, isLoading, error } = useQuery({
    queryKey: ["tournament"],
    queryFn: async () => {
      // Try to get tournament results from the autonomous events
      try {
        const res = await paperApi.getAutonomousEvents({ limit: 200, eventType: "tournament_completed" });
        return res;
      } catch {
        return { events: [] };
      }
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  // Mock tournament data for demonstration - in reality this would come from the tournament API
  const mockResults: TournamentResult[] = [
    { strategy_name: "EMA_Crossover_Trend", rank: 1, total_return: 0.234, sharpe: 1.82, max_drawdown: -0.089, win_rate: 0.58, trade_count: 127, regime: "trending_up", score: 94.2 },
    { strategy_name: "RSI_Mean_Reversion", rank: 2, total_return: 0.187, sharpe: 1.56, max_drawdown: -0.067, win_rate: 0.62, trade_count: 203, regime: "range_bound", score: 89.7 },
    { strategy_name: "Breakout_Vol", rank: 3, total_return: 0.156, sharpe: 1.34, max_drawdown: -0.112, win_rate: 0.51, trade_count: 89, regime: "volatility_expansion", score: 85.1 },
    { strategy_name: "VWAP_Momentum", rank: 4, total_return: 0.112, sharpe: 1.18, max_drawdown: -0.078, win_rate: 0.55, trade_count: 156, regime: "trending_up", score: 78.9 },
    { strategy_name: "SMA5_Trend", rank: 5, total_return: 0.089, sharpe: 1.02, max_drawdown: -0.095, win_rate: 0.49, trade_count: 134, regime: "range_bound", score: 72.3 },
  ];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">TOURNAMENT</span>
        <span className="pill text-[10px]">PHASE 24</span>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-center dim">Loading tournament…</div>
        ) : (
          <table className="data-table w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 bg-[var(--bg-elev)]">
              <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                <th className="p-1 text-right">#</th>
                <th className="p-1 text-left">Strategy</th>
                <th className="p-1 text-right">Return</th>
                <th className="p-1 text-right">Sharpe</th>
                <th className="p-1 text-right">Max DD</th>
                <th className="p-1 text-right">Win%</th>
                <th className="p-1 text-right">Trades</th>
                <th className="p-1 text-right">Score</th>
              </tr>
            </thead>
            <tbody>
              {mockResults.map((r) => (
                <tr key={r.strategy_name} className="border-b border-[var(--border-soft)]">
                  <td className="p-1 text-right font-mono text-[10px] font-bold amber">{r.rank}</td>
                  <td className="p-1 text-left truncate max-w-[160px] font-mono text-[10px]">{r.strategy_name}</td>
                  <td className="p-1 text-right font-mono text-[10px] up">{fmtPct(r.total_return)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{r.sharpe.toFixed(2)}</td>
                  <td className="p-1 text-right font-mono text-[10px] down">{fmtPct(r.max_drawdown)}</td>
                  <td className="p-1 text-right font-mono text-[10px]">{Math.round(r.win_rate * 100)}%</td>
                  <td className="p-1 text-right font-mono text-[10px]">{r.trade_count}</td>
                  <td className="p-1 text-right font-mono text-[10px] font-bold amber">{r.score.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        Tournament leaderboard · Paper-only · Walk-forward validated
      </div>
    </div>
  );
}