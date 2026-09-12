"use client";

import { useQuery } from "@tanstack/react-query";
import { paperApi } from "@/lib/paperApi";
import { type WidgetInstance } from "@/store/terminal";
import { RegimeResponse, AllocationResponse, Phase22Regime } from "@/types/paper-api";

const REGIME_COLORS: Record<Phase22Regime, { bg: string; text: string; border: string }> = {
  trending_up: { bg: "rgba(46,194,126,0.15)", text: "#2ec27e", border: "rgba(46,194,126,0.4)" },
  trending_down: { bg: "rgba(255,92,108,0.15)", text: "#ff5c6c", border: "rgba(255,92,108,0.4)" },
  range_bound: { bg: "rgba(242,179,71,0.15)", text: "#f2b347", border: "rgba(242,179,71,0.4)" },
  high_volatility: { bg: "rgba(255,92,108,0.1)", text: "#ff5c6c", border: "rgba(255,92,108,0.3)" },
  low_volatility: { bg: "rgba(76,141,255,0.1)", text: "#4c8dff", border: "rgba(76,141,255,0.3)" },
  volatility_expansion: { bg: "rgba(242,179,71,0.1)", text: "#f2b347", border: "rgba(242,179,71,0.3)" },
  volatility_contraction: { bg: "rgba(46,194,126,0.1)", text: "#2ec27e", border: "rgba(46,194,126,0.3)" },
  unknown: { bg: "rgba(125,138,160,0.1)", text: "#7d8aa0", border: "rgba(125,138,160,0.3)" },
};

const REGIME_LABELS: Record<Phase22Regime, string> = {
  trending_up: "TRENDING UP ▲",
  trending_down: "TRENDING DOWN ▼",
  range_bound: "RANGE BOUND ◄►",
  high_volatility: "HIGH VOLATILITY ⚡",
  low_volatility: "LOW VOLATILITY ⟳",
  volatility_expansion: "VOL EXPANSION ↗",
  volatility_contraction: "VOL CONTRACTION ↘",
  unknown: "UNKNOWN ?",
};

export function MarketRegimeWidget({ widget }: { widget: WidgetInstance }) {
  const { data: regimeData, error } = useQuery({
    queryKey: ["market-regime"],
    queryFn: () => paperApi.getRegime({ symbol: "NSE:NIFTY50", timeframe: "1d" }),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const { data: allocationData } = useQuery({
    queryKey: ["strategy-allocation"],
    queryFn: () => paperApi.getAllocation({ symbol: "NSE:NIFTY50", timeframe: "1d" }),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  // Type guards for ApiResult.ok === true
  const isRegimeResultOk = (result: typeof regimeData): result is { ok: true; data: RegimeResponse } => {
    return result !== undefined && result.ok === true;
  };

  const isAllocationResultOk = (result: typeof allocationData): result is { ok: true; data: AllocationResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract data safely
  const regimeDataSafe = isRegimeResultOk(regimeData) ? regimeData.data : undefined;
  const allocationDataSafe = isAllocationResultOk(allocationData) ? allocationData.data : undefined;

  const regime = regimeDataSafe?.regime ?? "unknown";
  const confidence = regimeDataSafe?.confidence ?? 0;
  const features = regimeDataSafe?.features ?? [];
  const warnings = regimeDataSafe?.warnings ?? [];

  const colors = REGIME_COLORS[regime as Phase22Regime] || REGIME_COLORS.unknown;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">MARKET REGIME</span>
        <span className="pill text-[10px]" style={{ backgroundColor: colors.bg, color: colors.text, borderColor: colors.border }}>
          {REGIME_LABELS[regime as Phase22Regime] || regime}
        </span>
      </div>

      <div className="p-3 shrink-0" style={{ backgroundColor: colors.bg, borderBottom: `1px solid ${colors.border}` }}>
        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <div className="dim text-[10px]">CONFIDENCE</div>
            <div className="font-bold text-[24px] font-mono" style={{ color: colors.text }}>
              {Math.round(confidence * 100)}%
            </div>
          </div>
          <div>
            <div className="dim text-[10px]">REGIME FIT</div>
            <div className="font-bold text-[24px] font-mono" style={{ color: colors.text }}>
              {allocationDataSafe?.regime_fit !== undefined
                ? Math.round(allocationDataSafe.regime_fit * 100) + "%"
                : "—"}
            </div>
          </div>
          <div>
            <div className="dim text-[10px]">STRATEGIES</div>
            <div className="font-bold text-[24px] font-mono" style={{ color: colors.text }}>
              {allocationDataSafe?.total_strategies_available ?? "—"}
            </div>
          </div>
        </div>

        {features.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1">
            {features.map((f, i) => (
              <span
                key={i}
                className="pill text-[9px] px-2 py-0.5"
                style={{ backgroundColor: colors.bg, color: colors.text, borderColor: colors.border }}
              >
                {f}
              </span>
            ))}
          </div>
        )}

        {warnings.length > 0 && (
          <div className="mt-2 p-2 text-[11px]" style={{ backgroundColor: "rgba(242,179,71,0.1)", border: "1px solid rgba(242,179,71,0.3)", borderRadius: 4, color: "#f2b347" }}>
            ⚠ {warnings.join(" · ")}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto p-2">
        {allocationDataSafe?.selected_strategies && allocationDataSafe.selected_strategies.length > 0 ? (
          <div>
            <div className="dim text-[10px] uppercase tracking-wider mb-2">RECOMMENDED ALLOCATION</div>
            <table className="data-table w-full" style={{ fontSize: 11 }}>
              <thead className="sticky top-0 bg-[var(--bg-elev)]">
                <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                  <th className="p-1 text-left">#</th>
                  <th className="p-1 text-left">Strategy</th>
                  <th className="p-1 text-right">Weight</th>
                  <th className="p-1 text-right">Regime Fit</th>
                  <th className="p-1 text-right">Research</th>
                </tr>
              </thead>
              <tbody>
                {allocationDataSafe.selected_strategies.map((s, i) => (
                  <tr key={s.strategy_name} className="border-b border-[var(--border-soft)]">
                    <td className="p-1 font-mono text-[10px]">{i + 1}</td>
                    <td className="p-1 text-left truncate max-w-[140px]">{s.strategy_name}</td>
                    <td className="p-1 text-right font-mono text-[10px]">{Math.round(s.weight * 100)}%</td>
                    <td className="p-1 text-right font-mono text-[10px]">{Math.round(s.regime_compatibility * 100)}%</td>
                    <td className="p-1 text-right font-mono text-[10px]">
                      {s.research_score !== null ? Math.round(s.research_score * 100) + "%" : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="p-4 text-center dim">
            No strategy allocation available. Market data provider may not be configured.
          </div>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        Updated: {regimeDataSafe?.regime_at_ms ? new Date(regimeDataSafe.regime_at_ms).toLocaleTimeString() : "—"}
        {error && <span className="ml-2 down">Error</span>}
      </div>
    </div>
  );
}