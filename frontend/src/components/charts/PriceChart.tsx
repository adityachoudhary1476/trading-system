import { useEffect, useRef, useState } from "react";
import type { OHLCVBar } from "@/types";
import { smaLine, emaLine, bollingerBands, vwap, rsi } from "@/lib/indicators";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type IPriceLine,
} from "lightweight-charts";

export type DrawTool = "none" | "trend" | "fib" | "forecast";

export interface IndicatorConfig {
  sma20: boolean;
  sma50: boolean;
  ema20: boolean;
  bb: boolean;
  vwap: boolean;
  rsi: boolean;
}

const DEFAULT_INDICATORS: IndicatorConfig = {
  sma20: false, sma50: false, ema20: false, bb: false, vwap: true, rsi: false,
};

const LW_THIN = 1 as 1;

type Anchor = { t: number; p: number }; // t in seconds (UTC), p = price
type Drawing =
  | { id: string; type: "trend" | "fib" | "forecast"; a: Anchor; b: Anchor };

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

export function PriceChart({
  data,
  indicators = DEFAULT_INDICATORS,
  levels = [],
  drawTool = "none",
  height = 460,
}: {
  data: OHLCVBar[];
  indicators?: IndicatorConfig;
  levels?: number[];
  drawTool?: DrawTool;
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const overlayRefs = useRef<ISeriesApi<"Line">[]>([]);
  const rsiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const priceLineRefs = useRef<IPriceLine[]>([]);

  const [legend, setLegend] = useState<{ o: number; h: number; l: number; c: number; chg: number } | null>(null);
  const [pending, setPending] = useState<Anchor | null>(null);
  const [tick, setTick] = useState(0); // forces SVG overlay recompute on pan/zoom
  const [size, setSize] = useState({ w: 800, h: height });

  // Create chart once
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: "white" }, textColor: "#9aa7b8", fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
      grid: { vertLines: { color: "rgba(31,38,51,0.45)" }, horzLines: { color: "rgba(31,38,51,0.45)" } },
      rightPriceScale: { borderColor: "#1f2633" },
      timeScale: { borderColor: "#1f2633", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1, vertLine: { color: "#3a4658", width: 1, style: 3, labelBackgroundColor: "#222b3a" }, horzLine: { color: "#3a4658", width: 1, style: 3, labelBackgroundColor: "#222b3a" } },
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#2ec27e", downColor: "#ff5c6c", borderUpColor: "#2ec27e", borderDownColor: "#ff5c6c",
      wickUpColor: "#2ec27e", wickDownColor: "#ff5c6c", priceLineVisible: false,
    });
    const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", color: "#2a3340" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(candle) as CandlestickData | undefined;
      setLegend(d ? { o: d.open as number, h: d.high as number, l: d.low as number, c: d.close as number, chg: (((d.close as number) - (d.open as number)) / (d.open as number)) * 100 } : null);
      setTick((t) => (t + 1) % 1_000_000);
    });
    chart.timeScale().subscribeVisibleTimeRangeChange(() => setTick((t) => (t + 1) % 1_000_000));
    chartRef.current = chart; candleRef.current = candle; volRef.current = vol;
    const ro = new ResizeObserver(() => { const r = el.getBoundingClientRect(); setSize({ w: r.width, h: r.height }); });
    ro.observe(el);
    return () => {
      // Strict cleanup: remove overlays/RSI/price-lines before tearing down the
      // whole chart. lightweight-charts v5 throws "Value is undefined" if you call
      // removeSeries on an already-removed series (StrictMode double-invoke / effect
      // re-run), so guard every removal here.
      try {
        overlayRefs.current.forEach((s) => { try { chart.removeSeries(s); } catch {} });
        overlayRefs.current = [];
        if (rsiRef.current) { try { chart.removeSeries(rsiRef.current); } catch {} rsiRef.current = null; }
        priceLineRefs.current.forEach((pl) => { try { candle.removePriceLine(pl); } catch {} });
        priceLineRefs.current = [];
      } finally {
        ro.disconnect();
        chart.remove();
        chartRef.current = null;
      }
    };
  }, []); // eslint-disable-line

  // Rebuild overlays + RSI when indicators or data change
  useEffect(() => {
    const chart = chartRef.current, candle = candleRef.current, vol = volRef.current;
    if (!chart || !candle || !vol) return;
    overlayRefs.current.forEach((s) => chart.removeSeries(s)); overlayRefs.current = [];
    if (rsiRef.current) { chart.removeSeries(rsiRef.current); rsiRef.current = null; }

    const candles = data.map((d) => ({ time: (d.time / 1000) as UTCTimestamp, open: d.open, high: d.high, low: d.low, close: d.close })) as CandlestickData[];
    const volData = data.map((d) => ({ time: (d.time / 1000) as UTCTimestamp, value: d.volume, color: d.close >= d.open ? "rgba(46,194,126,0.38)" : "rgba(255,92,108,0.38)" })) as HistogramData[];
    candle.setData(candles); vol.setData(volData);

    const addOverlay = (pts: { time: number; value: number }[], color: string, width: 1 | 2 | 3 | 4 = LW_THIN) => {
      const s = chart.addSeries(LineSeries, { color, lineWidth: width, priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: true });
      s.setData(pts as LineData[]); overlayRefs.current.push(s);
    };
    if (indicators.sma20) addOverlay(smaLine(data, 20), "#e0a44a");
    if (indicators.sma50) addOverlay(smaLine(data, 50), "#b388eb");
    if (indicators.ema20) addOverlay(emaLine(data, 20), "#5b8def");
    if (indicators.vwap) addOverlay(vwap(data), "#4dd0e1");
    if (indicators.bb) {
      const bb = bollingerBands(data, 20, 2);
      addOverlay(bb.upper, "rgba(120,140,170,0.5)"); addOverlay(bb.middle, "rgba(120,140,170,0.5)"); addOverlay(bb.lower, "rgba(120,140,170,0.5)");
    }
    if (indicators.rsi) {
      const r = chart.addSeries(LineSeries, { color: "#ffb74d", lineWidth: LW_THIN, priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: true }, 1);
      r.priceScale().applyOptions({ scaleMargins: { top: 0.15, bottom: 0.05 } });
      r.setData(rsi(data, 14) as LineData[]);
      chart.panes()[1]?.setHeight(110);
      r.createPriceLine({ price: 70, color: "rgba(255,92,108,0.35)", lineWidth: LW_THIN, lineStyle: 2, axisLabelVisible: false, title: "" });
      r.createPriceLine({ price: 30, color: "rgba(46,194,126,0.35)", lineWidth: LW_THIN, lineStyle: 2, axisLabelVisible: false, title: "" });
      rsiRef.current = r;
    }
    const last = data[data.length - 1];
    if (last) {
      const pl = candle.createPriceLine({ price: last.close, color: last.close >= last.open ? "#2ec27e" : "#ff5c6c", lineWidth: LW_THIN, lineStyle: 2, axisLabelVisible: true, title: "" });
      priceLineRefs.current.push(pl);
    }
    chart.timeScale().fitContent();
  }, [data, indicators]);

  // Sync horizontal user levels
  useEffect(() => {
    const candle = candleRef.current; if (!candle) return;
    priceLineRefs.current.forEach((pl) => candle.removePriceLine(pl)); priceLineRefs.current = [];
    levels.forEach((lvl) => priceLineRefs.current.push(candle.createPriceLine({ price: lvl, color: "#7d8aa0", lineWidth: LW_THIN, lineStyle: 0, axisLabelVisible: true, title: `L ${lvl.toFixed(2)}` })));
  }, [levels]);

  // Reset pending point when tool changes
  useEffect(() => { setPending(null); }, [drawTool]);

  const toXY = (a: Anchor): { x: number; y: number } | null => {
    const chart = chartRef.current, candle = candleRef.current; if (!chart || !candle) return null;
    const x = chart.timeScale().timeToCoordinate(a.t as UTCTimestamp);
    const y = candle.priceToCoordinate(a.p);
    if (x == null || y == null) return null;
    return { x, y };
  };

const DRAWINGS: Drawing[] = [];

  const svg = (() => {
    const chart = chartRef.current; if (!chart) return null;
    const w = size.w, h = size.h;
    return (
      <svg key={tick} width={w} height={h} style={{ position: "absolute", inset: 0, pointerEvents: "none" }} aria-hidden="true">
        {DRAWINGS.map((d) => {
          const A = toXY(d.a), B = toXY(d.b);
          if (!A || !B) return null;
          if (d.type === "trend") {
            return <line key={d.id} x1={A.x} y1={A.y} x2={B.x} y2={B.y} stroke="#7aa2ff" strokeWidth={1.5} strokeDasharray="0" />;
          }
          if (d.type === "fib") {
            const top = Math.max(d.a.p, d.b.p), bot = Math.min(d.a.p, d.b.p);
            const x0 = Math.min(A.x, B.x), x1 = Math.max(A.x, B.x);
            return (
              <g key={d.id}>
                <line x1={A.x} y1={A.y} x2={B.x} y2={B.y} stroke="#c9a227" strokeWidth={1} />
                {FIB_LEVELS.map((lv) => {
                  const price = top - (top - bot) * lv;
                  const y = candleRef.current?.priceToCoordinate(price);
                  if (y == null) return null;
                  return (
                    <g key={lv}>
                      <line x1={x0} y1={y} x2={x1} y2={y} stroke="rgba(201,162,39,0.55)" strokeWidth={1} />
                      <text x={x1 + 4} y={y - 2} fill="#c9a227" fontSize={10}>{lv === 0 ? "100%" : lv === 1 ? "0%" : `${(lv * 100).toFixed(1)}%`} {price.toFixed(2)}</text>
                    </g>
                  );
                })}
              </g>
            );
          }
          // forecast: rectangle extended forward + dashed projection
          const dur = Math.max(1, d.b.t - d.a.t);
          const ext = toXY({ t: d.b.t + dur, p: d.b.p + (d.b.p - d.a.p) });
          const topY = Math.min(A.y, B.y, ext?.y ?? B.y);
          const botY = Math.max(A.y, B.y, ext?.y ?? B.y);
          const x0 = A.x, x1 = ext?.x ?? B.x;
          return (
            <g key={d.id}>
              <rect x={x0} y={topY} width={Math.max(0, x1 - x0)} height={Math.max(0, botY - topY)} fill="rgba(122,162,255,0.07)" stroke="rgba(122,162,255,0.4)" strokeWidth={1} strokeDasharray="4 3" />
              {ext && <line x1={B.x} y1={B.y} x2={ext.x} y2={ext.y} stroke="#7aa2ff" strokeWidth={1.5} strokeDasharray="6 4" />}
            </g>
          );
        })}
        {pending && (() => { const P = toXY(pending); if (!P) return null; return <circle cx={P.x} cy={P.y} r={4} fill="#7aa2ff" stroke="#fff" strokeWidth={1} />; })()}
      </svg>
    );
  })();

  const activeIndicatorCount = Object.values(indicators).filter(Boolean).length;
  return (
    <div style={{ position: "relative", width: "100%", height }}>
      <div ref={containerRef} style={{ width: "100%", height }} />
      {svg}
      {drawTool !== "none" && (
        <div className="chart-draw-hint" aria-hidden="true">
          {drawTool === "trend" ? "Trendline" : drawTool === "fib" ? "Fibonacci" : "Forecast"}: click two points
        </div>
      )}
      {legend && (
        <div className="chart-legend" aria-hidden="true">
          <span>O <b>{legend.o.toFixed(2)}</b></span><span>H <b>{legend.h.toFixed(2)}</b></span>
          <span>L <b>{legend.l.toFixed(2)}</b></span><span>C <b>{legend.c.toFixed(2)}</b></span>
          <span className={legend.chg >= 0 ? "pos" : "neg"}>{legend.chg >= 0 ? "+" : ""}{legend.chg.toFixed(2)}%</span>
        </div>
      )}
      {!activeIndicatorCount && drawTool === "none" && (
        <span className="chart-hint" aria-hidden="true">+ SMA · EMA · BB · VWAP · RSI</span>
      )}
    </div>
  );
}

export { DEFAULT_INDICATORS };
