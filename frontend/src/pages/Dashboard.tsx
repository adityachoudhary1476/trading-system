import { useEffect, useState } from "react";
import { useApp } from "@/store/AppContext";
import { dataSource } from "@/data/MarketDataSource";
import type { OHLCVBar } from "@/types";
import { PriceChart } from "@/components/charts/PriceChart";
import { QuoteHeader, MetricsPanel } from "@/components/market/QuoteAndMetrics";
import { AIAnalysisPanel, SignalCard } from "@/components/ai/AIAnalysis";
import { DataHealthPanel } from "@/components/system/DataHealthPanel";
import { MarketStatusPanel } from "@/components/layout/MarketStatusPanel";
import { Panel, Badge, Loading } from "@/components/ui";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1D"];

export function DashboardPage() {
  const { selectedSymbol } = useApp();
  const [tf, setTf] = useState("5m");
  const [bars, setBars] = useState<OHLCVBar[] | null>(null);

  useEffect(() => {
    let alive = true;
    setBars(null);
    dataSource.getOHLCV(selectedSymbol, tf, 160).then((r) => alive && setBars(r));
    return () => { alive = false; };
  }, [selectedSymbol, tf]);

  return (
    <>
      <QuoteHeader symbol={selectedSymbol} />
      <div className="grid" style={{ gridTemplateColumns: "1fr 320px", gap: 16, alignItems: "start" }}>
        <div className="grid" style={{ gap: 16 }}>
          <Panel
            title="Price Action"
            actions={
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
            }
          >
            {bars ? <PriceChart data={bars} /> : <Loading label="Loading chart…" />}
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

void Badge;
