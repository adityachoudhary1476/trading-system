import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { AIAnalysis } from "@/types";
import { Badge, Panel } from "@/components/ui";
import { fmtPct } from "@/lib/format";

export function AIAnalysisPanel({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getAIAnalysis(symbol).then((r) => alive && setAi(r));
    return () => { alive = false; };
  }, [symbol]);

  if (!ai) return null;
  return (
    <Panel
      title="AI Market Intelligence"
      actions={<Badge kind={ai.bias}>{ai.bias.toUpperCase()}</Badge>}
    >
      <div className="bias-readout">
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>Bias</div>
          <div className={`bias-pill ${ai.bias === "bullish" ? "pos" : ai.bias === "bearish" ? "neg" : "muted"}`}>
            {ai.bias.toUpperCase()}
          </div>
        </div>
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>Confidence</div>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{fmtPct(ai.confidence * 100, 0).replace("%", "")}%</div>
        </div>
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>Signal</div>
          <div style={{ fontSize: 18, fontWeight: 700, textTransform: "uppercase" }}>
            <Badge kind={ai.signal}>{ai.signal.replace("_", " ")}</Badge>
          </div>
        </div>
      </div>

      <div className="factors">
        {ai.factors.map((f) => (
          <div className="factor" key={f.label}>
            <span className="fk">{f.label}</span>
            <span className={`fv ${f.tone === "positive" ? "pos" : f.tone === "negative" ? "neg" : f.tone === "warning" ? "" : "muted"}`}>{f.value}</span>
          </div>
        ))}
      </div>

      <div className="ai-summary">{ai.summary}</div>
      <p className="faint" style={{ fontSize: 11, marginBottom: 0, marginTop: 12 }}>
        Mock AI output ({ai.model}). Powered by the same MarketView/Snapshot contract the real analyst will populate.
      </p>
    </Panel>
  );
}

export function SignalCard({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getAIAnalysis(symbol).then((r) => alive && setAi(r));
    return () => { alive = false; };
  }, [symbol]);
  if (!ai) return null;
  return (
    <Panel title="Signal" actions={<Badge kind={ai.signal}>{ai.signal.replace("_", " ")}</Badge>}>
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div className="stat">
          <span className="label">Confidence</span>
          <span className="value">{fmtPct(ai.confidence * 100, 0).replace("%", "")}%</span>
        </div>
        <div className="stat">
          <span className="label">Bias</span>
          <span className="value" style={{ textTransform: "capitalize" }}>{ai.bias}</span>
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        Analytical signal only. No order is placed — execution is disabled in this build.
      </p>
    </Panel>
  );
}
