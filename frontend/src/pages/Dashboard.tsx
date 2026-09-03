import { useEffect, useRef, useState } from "react";
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
import { useMarketStatus } from "@/data/useQuote";
import { fmtTimeIST } from "@/lib/format";

// India Standard Time is UTC+5:30. Upstox timestamps NSE candles at
// 00:00 local (IST), which in epoch-ms UTC is 18:30 of the *previous*
// calendar day. A naive Math.floor(ts / barMs) therefore maps a daily
// candle and a live tick on the same trading day onto different UTC
// calendar days, causing a phantom bar to be appended every second
// instead of updating the in-progress candle. We shift by the IST offset
// before flooring and shift back so boundaries align to local-midnight.
export const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

/**
 * Merge a live tick into the in-progress bar.  If the tick is for a
 * bar in the future or a bar older than the last historical one, the
 * array is left untouched.  This is what makes the chart look "live"
 * without an extra fetch per second.
 */
export function mergeLiveTick(bars: OHLCVBar[] | null, price: number, ts: number, barMs: number): OHLCVBar[] | null {
  if (!bars || !Number.isFinite(price) || price <= 0 || !Number.isFinite(ts) || ts <= 0 || !barMs) {
    return bars;
  }
  const last = bars[bars.length - 1];
  if (!last) return bars;
  const lastStart = floorToBar(last.time, barMs);
  const tickStart = floorToBar(ts, barMs);
  if (tickStart < lastStart) return bars;
  if (tickStart === lastStart) {
    const updated: OHLCVBar = {
      ...last,
      close: price,
      high: Math.max(last.high, price),
      low: Math.min(last.low, price),
    };
    return [...bars.slice(0, -1), updated];
  }
  // Tick is for a brand new (in-progress) bar beyond the historicals.
  // Append it so the chart can show the open of the next bar.
  const next: OHLCVBar = {
    time: tickStart,
    open: price,
    high: price,
    low: price,
    close: price,
    volume: 0,
  };
  return [...bars, next];
}

export function floorToBar(ts: number, barMs: number): number {
  return Math.floor((ts + IST_OFFSET_MS) / barMs) * barMs - IST_OFFSET_MS;
}

// Timeframes exposed to the operator. Each entry is a key that the
// Upstox-backed Vercel OHLCV handler accepts verbatim (see
// `frontend/api/market/ohlcv.ts` INTERVAL_MAP). Do NOT add entries that the
// handler will reject with 400 â€” unsupported timeframes produce empty charts
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
  const readModelVersion = useRef(-1);

  useEffect(() => {
    let alive = true;
    setBars(null);
    setLoading(true);
    setError(null);
    readModelVersion.current = -1;
    const load = dataSource.getCandleReadModel
      ? dataSource.getCandleReadModel(selectedSymbol, tf, 160).catch(() =>
          dataSource
            .getOHLCV(selectedSymbol, tf, 160)
            .then((candles) => ({ candles, version: 0 })),
        )
      : dataSource.getOHLCV(selectedSymbol, tf, 160).then((candles) => ({ candles, version: 0 }));
    load
      .then((model) => {
        if (!alive) return;
        if (model.version < readModelVersion.current) return;
        readModelVersion.current = model.version;
        setBars(model.candles);
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

  const session = useMarketStatus();

  // Periodically refresh OHLCV during market hours so the historical
  // series does not go stale.  The live tick merge (below) smooths the
  // in-progress bar, but the last closed candle never updates until
  // the next fetch.  We poll only while the market is open to avoid
  // unnecessary calls on weekends/holidays.
  useEffect(() => {
    const phase = session?.phase ?? "closed";
    const isOpen = phase === "regular" || phase === "pre_market" || phase === "post_market";
    if (!session || !isOpen) return;
    let alive = true;
    // Refresh the authoritative read model during market hours.
    const id = setInterval(() => {
      const load = dataSource.getCandleReadModel
        ? dataSource.getCandleReadModel(selectedSymbol, tf, 160).catch(() =>
            dataSource
              .getOHLCV(selectedSymbol, tf, 160)
              .then((candles) => ({ candles, version: 0 })),
          )
        : dataSource.getOHLCV(selectedSymbol, tf, 160).then((candles) => ({ candles, version: 0 }));
      load
        .then((model) => {
          if (!alive || model.version < readModelVersion.current) return;
          readModelVersion.current = model.version;
          setBars(model.candles);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          setError(err instanceof Error ? err.message : "Failed to load chart data");
          setLoading(false);
        });
    }, 1_000);
    return () => { alive = false; clearInterval(id); };
  }, [selectedSymbol, tf, session?.phase]);

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
      {session && (session.phase === "closed" || session.phase === "holiday") ? (
        <div className="panel" data-testid="market-closed-banner" style={{ marginBottom: 12 }}>
          <div className="panel-head">
            <span className="panel-title">Market {session.phase === "holiday" ? "Holiday" : "Closed"}</span>
            <span className="faint" style={{ fontSize: 11 }}>
              {session.nextOpen ? `Next open ${fmtTimeIST(session.nextOpen)}` : "Awaiting next session"}
            </span>
          </div>
          <p className="faint" style={{ fontSize: 12, margin: 0 }}>
            Showing last known prices; live ticks are paused until the next NSE session.
          </p>
        </div>
      ) : null}
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
                <div className="empty-icon" aria-hidden="true">âš </div>
                <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Unable to load chart</div>
                <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
              </div>
            ) : (
              <Loading label="Loading chartâ€¦" />
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
        Historical OHLCV is sourced from Upstox via the Vercel /api/market/ohlcv route. The live tick merge (below) updates the in-progress bar; live price updates every second via /api/market/quote.
      </p>
    </>
  );
}



