import { useEffect, useRef } from "react";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { OHLCVBar } from "@/types";

/**
 * Candlestick + volume chart driven entirely by OHLCVBar[] provided by the
 * data source. The component does NOT generate data — it renders whatever the
 * MarketDataSource returns (mock tonight, real API/WS tomorrow).
 */
export function PriceChart({ data, height = 420 }: { data: OHLCVBar[]; height?: number }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: "#9aa7b8",
        fontFamily: "Inter, system-ui, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(31,38,51,0.5)" },
        horzLines: { color: "rgba(31,38,51,0.5)" },
      },
      rightPriceScale: { borderColor: "#1f2633" },
      timeScale: { borderColor: "#1f2633", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#2ec27e",
      downColor: "#ff5c6c",
      borderUpColor: "#2ec27e",
      borderDownColor: "#ff5c6c",
      wickUpColor: "#2ec27e",
      wickDownColor: "#ff5c6c",
    });
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      color: "#2a3340",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    chartRef.current = chart;
    candleRef.current = candle;
    volRef.current = vol;
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!candleRef.current || !volRef.current) return;
    const candleData = data.map((d) => ({
      time: (d.time / 1000) as UTCTimestamp,
      open: d.open,
      high: d.high,
      low: d.low,
      close: d.close,
    }));
    const volData = data.map((d) => ({
      time: (d.time / 1000) as UTCTimestamp,
      value: d.volume,
      color: d.close >= d.open ? "rgba(46,194,126,0.4)" : "rgba(255,92,108,0.4)",
    }));
    candleRef.current.setData(candleData);
    volRef.current.setData(volData);
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  return <div ref={containerRef} style={{ width: "100%", height }} />;
}
