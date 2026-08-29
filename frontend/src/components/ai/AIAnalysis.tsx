import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import type { AIAnalysis } from "@/types";
import { Badge, Panel } from "@/components/ui";
import { fmtPrice, fmtTime } from "@/lib/format";

export function AIAnalysisPanel({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getAIAnalysis(symbol).then((r) => alive && setAi(r));
    return () => { alive = false; };
  }, [symbol]);

  if (!ai) return null;
  const confPct = Math.round(ai.confidence * 100);
  return (
    <Panel
      title="AI Market Intelligence"
      actions={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <Badge kind={ai.bias}>{ai.bias.toUpperCase()}</Badge>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-faint)" }}>
            · on candle close
          </span>
        </span>
      }
    >
      <div className="bias-readout">
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>
            Bias
          </div>
          <div className={`bias-pill ${ai.bias === "bullish" ? "pos" : ai.bias === "bearish" ? "neg" : "muted"}`}>
            {ai.bias.toUpperCase()}
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 120 }}>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 4 }}>
            Confidence
          </div>
          <div className="conf-bar" role="progressbar" aria-valuenow={confPct} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${confPct}%` }} className={ai.bias === "bearish" ? "neg" : ai.bias === "bullish" ? "pos" : "muted"} />
          </div>
          <div className="mono" style={{ fontSize: 12, marginTop: 4 }}>{confPct}%</div>
        </div>
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>
            Signal
          </div>
          <div style={{ fontSize: 18, fontWeight: 700, textTransform: "uppercase" }}>
            <Badge kind={ai.signal}>{ai.signal.replace("_", " ")}</Badge>
          </div>
        </div>
      </div>

      <div className="factors">
        {ai.factors.map((f) => (
          <div className="factor" key={f.label}>
            <span className="fk">{f.label}</span>
            <span className={`fv ${f.tone === "positive" ? "pos" : f.tone === "negative" ? "neg" : f.tone === "warning" ? "warn" : "muted"}`}>
              {f.value}
            </span>
          </div>
        ))}
      </div>

      <div className="ai-summary">{ai.summary}</div>
      <p className="faint" style={{ fontSize: 11, marginBottom: 0, marginTop: 12 }}>
        Mock AI output ({ai.model}). Analysis computed on <b>closed candles</b>, not every tick — matching the
        backend architecture. Ready to be populated by the real MarketView/Snapshot contract.
      </p>
    </Panel>
  );
}

export function SignalCard({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  const [sig, setSig] = useState<{ price: number; generatedAt: number; reason: string } | null>(null);
  useEffect(() => {
    let alive = true;
    dataSource.getAIAnalysis(symbol).then((r) => alive && setAi(r));
    dataSource.getSignals(12).then((list) => {
      if (!alive) return;
      const s = list.find((x) => x.symbol === symbol) ?? list[0];
      if (s) setSig({ price: s.price, generatedAt: s.generatedAt, reason: s.reason });
    });
    return () => { alive = false; };
  }, [symbol]);
  if (!ai) return null;
  const confPct = Math.round(ai.confidence * 100);
  return (
    <Panel title="Signal" actions={<Badge kind={ai.signal}>{ai.signal.replace("_", " ")}</Badge>}>
      <div className="signal-emphasis">
        <span className={`signal-word sig-${ai.signal}`}>{ai.signal.replace("_", " ")}</span>
        <span className="faint" style={{ fontSize: 11 }}>generated on candle close</span>
      </div>
      <div className="grid cols-2" style={{ gap: 12, marginTop: 12 }}>
        <div className="stat">
          <span className="label">Confidence</span>
          <span className="value">{confPct}%</span>
        </div>
        <div className="stat">
          <span className="label">Bias</span>
          <span className="value" style={{ textTransform: "capitalize" }}>{ai.bias}</span>
        </div>
        <div className="stat">
          <span className="label">Price at Signal</span>
          <span className="value mono" style={{ fontSize: 14 }}>₹{fmtPrice(sig?.price ?? 0)}</span>
        </div>
        <div className="stat">
          <span className="label">Generated</span>
          <span className="value mono" style={{ fontSize: 13 }}>{sig ? fmtTime(sig.generatedAt) : "—"}</span>
        </div>
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        Analytical signal only. No order is placed — execution is disabled in this build.
      </p>
    </Panel>
  );
}
