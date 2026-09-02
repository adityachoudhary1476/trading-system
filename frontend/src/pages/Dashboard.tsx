import { useEffect, useState } from "react";
import { useApp } from "@/store/AppContext";
import { dataSource } from "@/data/MarketDataSource";
import type { OHLCVBar } from "@/types";
import { PriceChart, DEFAULT_INDICATORS, type IndicatorConfig, type DrawTool } from "@/components/charts/PriceChart";
import { QuoteHeader, MetricsPanel, MetricsStrip } from "@/components/market/QuoteAndMetrics";
import { AIAnalysisPanel, SignalCard } from "@/components/ai/AIAnalysis";
import { DataHealthPanel } from "@/components/system/DataHealthPanel";
import { MarketStatusPanel } from "@/components/layout/MarketStatusPanel";
import { TickerStrip } from "@/components/market/TickerStrip";
import { Panel, Loading } from "@/components/ui";

// Timeframes exposed to the operator. Each entry is a key that the
// Upstox-backed Vercel OHLCV handler accepts verbatim (see
// `frontend/api/market/ohlcv.ts` INTERVAL_MAP). Do NOT add entries that the
// handler will reject with 400 — unsupported timeframes produce empty charts
// and force the user to guess the supported set.
const TIMEFRAMES = ["1m", "1D", "1W", "1M"] as const;
const INDICATOR_KEYS: { key: keyof IndicatorConfig; label: string }[] = [
  { key: "sma20", label: "SMA 20" },
  { key: "sma50", label: "SMA 50" },
  { key: "ema20", label: "EMA 20" },
  { key: "bb", label: "BB" },
  { key: "vwap", label: "VWAP" },
  { key: "rsi", label: "RSI" },
];

export function DashboardPage() {
  const { selectedSymbol } = useApp();
  // Default to a timeframe that the Vercel OHLCV handler actually supports.
  // Choosing an unsupported default (e.g. "5m") would cause the chart to
  // mount empty on first paint. The default value is also constrained to
  // exist in TIMEFRAMES below; an unsupported default would also let
  // operators pick buttons that 400 the API.
  const [tf, setTf] = useState<string>("1D");
  const [bars, setBars] = useState<OHLCVBar[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [indicators, setIndicators] = useState<IndicatorConfig>(DEFAULT_INDICATORS);
  const [levels, setLevels] = useState<number[]>([]);
  const [drawTool, setDrawTool] = useState<DrawTool>("none");

  useEffect(() => {
    let alive = true;
    setBars(null);
    setLoading(true);
    setError(null);
    dataSource
      .getOHLCV(selectedSymbol, tf, 160)
      .then((r) => {
        if (!alive) return;
        setBars(r);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : "Failed to load chart data");
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selectedSymbol, tf]);

  const lastClose = bars && bars.length ? bars[bars.length - 1].close : 0;
  const toggleInd = (k: keyof IndicatorConfig) =>
    setIndicators((prev) => ({ ...prev, [k]: !prev[k] }));
  const addLevel = () => { if (lastClose) setLevels((p) => [...p, lastClose]); };
  const clearLevels = () => setLevels([]);
  const DRAW_KEYS: { key: DrawTool; label: string }[] = [
    { key: "trend", label: "Trend" },
    { key: "fib", label: "Fib" },
    { key: "forecast", label: "Forecast" },
    { key: "none", label: "Off" },
  ];
  const toggleDraw = (k: DrawTool) => setDrawTool((prev) => (prev === k && k !== "none" ? "none" : k));

  return (
    <>
      {loading && <div className="topbar-loading" role="progressbar" aria-label="Loading market data" />}
      <TickerStrip />
      <QuoteHeader symbol={selectedSymbol} />
      <MetricsStrip symbol={selectedSymbol} />
      <div className="grid" style={{ gridTemplateColumns: "1fr 340px", gap: 16, alignItems: "start" }}>
        <div className="grid" style={{ gap: 16 }}>
          <Panel
            title="Price Action"
            toolbar={
              <div className="chart-toolbar" role="group" aria-label="Chart tools">
                <div className="tf-controls" role="group" aria-label="Timeframe">
                  {TIMEFRAMES.map((t) => (
                    <button
                      key={t}
                      className={t === tf ? "active" : ""}
                      onClick={() => setTf(t)}
                      aria-pressed={t === tf}
                    >
                      {t}
                    </button>
                  ))}
                </div>
                <span className="tb-sep" />
                <div className="ind-controls" role="group" aria-label="Indicators">
                  {INDICATOR_KEYS.map(({ key, label }) => (
                    <button
                      key={key}
                      className={indicators[key] ? "active" : ""}
                      onClick={() => toggleInd(key)}
                      aria-pressed={indicators[key]}
                      title={`Toggle ${label}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <span className="tb-sep" />
                <div className="ind-controls" role="group" aria-label="Draw tools">
                  {DRAW_KEYS.map(({ key, label }) => (
                    <button
                      key={key}
                      className={drawTool === key ? "active" : ""}
                      onClick={() => toggleDraw(key)}
                      aria-pressed={drawTool === key}
                      title={`Drawing tool: ${label}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <span className="tb-sep" />
                <button className="lvl-btn" onClick={addLevel} title="Add a horizontal price level at last close">
                  + Level
                </button>
                {levels.length > 0 && (
                  <button className="lvl-btn" onClick={clearLevels} title="Clear price levels">
                    Clear {levels.length}
                  </button>
                )}
              </div>
            }
          >
            {bars ? (
              <PriceChart
                data={bars}
                indicators={indicators}
                levels={levels}
                drawTool={drawTool}
              />
            ) : error ? (
              <div className="empty">
                <div className="empty-icon" aria-hidden="true">⚠</div>
                <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Unable to load chart</div>
                <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
              </div>
            ) : (
              <Loading label="Loading chart…" />
            )}
          </Panel>
          <Panel title="Market Metrics">
            <MetricsPanel symbol={selectedSymbol} />
          </Panel>
        </div>
        <div className="grid" style={{ gap: 16 }}>
          <AIAnalysisPanel symbol={selectedSymbol} />
          <SignalCard symbol={selectedSymbol} />
          <MarketStatusPanel />
          <DataHealthPanel />
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>
        Demo data. The chart consumes OHLCV from <code>MarketDataSource</code> — swap to a real API/WS source tomorrow without UI changes.
      </p>
    </>
  );
}

