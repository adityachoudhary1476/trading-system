import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { FeedHealth } from "@/types";
import { Panel, Badge, Loading, HealthDot } from "@/components/ui";
import { fmtAgo, fmtNum } from "@/lib/format";

export function DataHealthPanel() {
  const [h, setH] = useState<FeedHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setError(null);
    dataSource
      .getFeedHealth()
      .then((r) => {
        if (alive) setH(r);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load feed health");
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <Panel title="Data Health">
        <div className="empty">
          <div className="empty-icon" aria-hidden="true">⚠</div>
          <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Data health unavailable</div>
          <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
        </div>
      </Panel>
    );
  }

  if (!h) return <Panel title="Data Health"><Loading /></Panel>;
  const healthy = h.status === "healthy";
  const status = h.status ?? "disconnected";
  return (
    <Panel
      title="Data Health"
      actions={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <HealthDot status={status} pulse />
          <Badge kind={status}>{status.replace("_", " ")}</Badge>
        </span>
      }
    >
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div className="stat">
          <span className="label">Feed</span>
          <span className="value">{h.feed ?? "—"}</span>
        </div>
        <div className="stat">
          <span className="label">Last Tick</span>
          <span className="value mono" style={{ fontSize: 13 }}>{fmtAgo(h.lastTick)}</span>
        </div>
        <div className="stat">
          <span className="label">Events Received</span>
          <span className="value mono">{fmtNum(h.eventsReceived, { useGrouping: true })}</span>
        </div>
        <div className="stat">
          <span className="label">Events Rejected</span>
          <span className="value mono">{fmtNum(h.eventsRejected, { useGrouping: true })}</span>
        </div>
        <div className="stat">
          <span className="label">Candles Generated</span>
          <span className="value mono">{fmtNum(h.candlesGenerated, { useGrouping: true })}</span>
        </div>
        <div className="stat">
          <span className="label">Last Closed Candle</span>
          <span className="value mono" style={{ fontSize: 13 }}>{fmtAgo(h.lastClosedCandle)}</span>
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        {healthy ? "Pipeline is healthy." : "Feed issue detected — signals suppressed."} Mock metrics; real backend replaces these via FeedHealth.
      </p>
    </Panel>
  );
}
