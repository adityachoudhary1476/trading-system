import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { FeedHealth } from "@/types";
import { Panel, Badge, Loading, EmptyState } from "@/components/ui";
import { fmtTime, fmtHM } from "@/lib/format";

export function DataHealthPanel() {
  const [h, setH] = useState<FeedHealth | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getFeedHealth().then((r) => alive && setH(r));
    return () => { alive = false; };
  }, []);

  if (!h) return <Panel title="Data Health"><Loading /></Panel>;
  const healthy = h.status === "healthy";
  return (
    <Panel
      title="Data Health"
      actions={<Badge kind={h.status}>{h.status.replace("_", " ")}</Badge>}
    >
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div className="stat">
          <span className="label">Feed</span>
          <span className="value">{h.feed}</span>
        </div>
        <div className="stat">
          <span className="label">Last Tick</span>
          <span className="value mono" style={{ fontSize: 13 }}>{fmtTime(h.lastTick)}</span>
        </div>
        <div className="stat">
          <span className="label">Events Received</span>
          <span className="value mono">{h.eventsReceived.toLocaleString("en-IN")}</span>
        </div>
        <div className="stat">
          <span className="label">Events Rejected</span>
          <span className="value mono">{h.eventsRejected.toLocaleString("en-IN")}</span>
        </div>
        <div className="stat">
          <span className="label">Candles Generated</span>
          <span className="value mono">{h.candlesGenerated.toLocaleString("en-IN")}</span>
        </div>
        <div className="stat">
          <span className="label">Last Closed Candle</span>
          <span className="value mono" style={{ fontSize: 13 }}>{fmtHM(h.lastClosedCandle)}</span>
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        {healthy ? "Pipeline is healthy." : "Feed issue detected — signals suppressed."} Mock metrics; real backend replaces these via FeedHealth.
      </p>
    </Panel>
  );
}

void EmptyState;
