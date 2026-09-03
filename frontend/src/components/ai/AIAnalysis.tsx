import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import { usePriceDelta } from "@/data/useQuote";
import type { AIAnalysis } from "@/types";
import { Badge, Panel } from "@/components/ui";
import { fmtPrice, fmtTime, fmtPct, fmtDuration } from "@/lib/format";

export function AIAnalysisPanel({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Always call usePriceDelta so hook count stays constant regardless of
  // whether analysis loads, errors, or is still pending.
  const priceDelta = usePriceDelta(symbol, ai?.decisionPrice ?? null);
  useEffect(() => {
    let alive = true;
    setError(null);
    dataSource
      .getAIAnalysis(symbol)
      .then((r) => {
        if (alive) setAi(r);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load analysis");
      });
    return () => {
      alive = false;
    };
  }, [symbol]);

  if (error) {
    return (
      <Panel title="AI Market Intelligence">
        <div className="empty">
          <div className="empty-icon" aria-hidden="true">⚠</div>
          <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Analysis unavailable</div>
          <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
        </div>
      </Panel>
    );
  }

  if (!ai) return null;
  const confPct = Math.round((ai.confidence ?? 0) * 100);
  const signal = ai.signal ?? "no_signal";
  const horizon = ai.horizon ?? "short_term";
  const evidence = ai.evidence;
  const deltaPct = ai.decisionPrice && ai.decisionPrice !== 0 && priceDelta !== null
    ? (priceDelta / ai.decisionPrice) * 100
    : null;
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
          <div className="mono" style={{ fontSize: 12, marginTop: 4 }}>{confPct}/100</div>
        </div>
        <div>
          <div className="faint" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6 }}>
            Signal
          </div>
          <div style={{ fontSize: 18, fontWeight: 700, textTransform: "uppercase" }}>
            <Badge kind={signal}>{signal.replace("_", " ")}</Badge>
          </div>
        </div>
      </div>

      {/* Horizon and Expected Move */}
      <div style={{ display: "flex", gap: 16, marginTop: 12, padding: "8px 12px", background: "var(--surface-2)", borderRadius: 6 }}>
        <div>
          <div className="faint" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5 }}>Horizon</div>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{horizon.replace("_", " ")}</div>
        </div>
        {ai.expectedMove && (
          <div>
            <div className="faint" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5 }}>Expected Range</div>
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              {ai.expectedMove.lowerPct > 0 ? "+" : ""}{ai.expectedMove.lowerPct}% to {ai.expectedMove.upperPct > 0 ? "+" : ""}{ai.expectedMove.upperPct}%
            </div>
          </div>
        )}
        {ai.invalidation && (
          <div>
            <div className="faint" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5 }}>Invalidation</div>
            <div style={{ fontSize: 13, fontWeight: 600 }}>{ai.invalidation}</div>
          </div>
        )}
      </div>

      {/* Evidence Ledger */}
      {evidence && (
        <div style={{ marginTop: 12, padding: "8px 12px", background: "var(--surface-2)", borderRadius: 6 }}>
          <div className="faint" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
            Evidence: {evidence.agreement}
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {evidence.positive.length > 0 && (
              <div>
                <div style={{ fontSize: 11, color: "var(--positive)", fontWeight: 600 }}>Positive</div>
                {evidence.positive.map((e, i) => (
                  <div key={i} style={{ fontSize: 11, color: "var(--text-dim)" }}>• {e}</div>
                ))}
              </div>
            )}
            {evidence.negative.length > 0 && (
              <div>
                <div style={{ fontSize: 11, color: "var(--negative)", fontWeight: 600 }}>Negative</div>
                {evidence.negative.map((e, i) => (
                  <div key={i} style={{ fontSize: 11, color: "var(--text-dim)" }}>• {e}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

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

      {ai.decisionPrice != null && (
        <div style={{ marginTop: 12, padding: "8px 12px", background: "var(--surface-2)", borderRadius: 6, border: "1px solid var(--border-color)" }}>
          <div className="faint" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
            Decision Snapshot
          </div>
          <div className="grid cols-2" style={{ gap: 8 }}>
            <div className="stat">
              <span className="label">Decision Price</span>
              <span className="value mono">₹{fmtPrice(ai.decisionPrice)}</span>
            </div>
            <div className="stat">
              <span className="label">Based On</span>
              <span className="value mono" style={{ fontSize: 13 }}>{ai.marketTimestamp ? fmtTime(ai.marketTimestamp) : "—"}</span>
            </div>
            <div className="stat">
              <span className="label">Generated</span>
              <span className="value mono" style={{ fontSize: 13 }}>{ai.decisionTimestamp ? fmtTime(ai.decisionTimestamp) : "—"}</span>
            </div>
            <div className="stat">
              <span className="label">Data Freshness</span>
              <span className="value mono" style={{ fontSize: 13 }}>{fmtDuration(ai.dataFreshnessMs)}</span>
            </div>
            {priceDelta !== null && (
              <div className="stat">
                <span className="label">Live Δ</span>
                <span className={`value mono ${priceDelta > 0 ? "pos" : priceDelta < 0 ? "neg" : "muted"}`}>
                  {priceDelta > 0 ? "+" : ""}₹{fmtPrice(priceDelta)} ({deltaPct != null ? `${deltaPct > 0 ? "+" : ""}${fmtPct(deltaPct)}` : "—"})
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      <p className="faint" style={{ fontSize: 11, marginBottom: 0, marginTop: 12 }}>
        AI-generated analytical output ({ai.model}). Confidence is analytical (0-100), NOT a probability of profit.
        Analysis computed on <b>closed candles</b>, not every tick.
      </p>
    </Panel>
  );
}

export function SignalCard({ symbol }: { symbol: string }) {
  const [ai, setAi] = useState<AIAnalysis | null>(null);
  const [sig, setSig] = useState<{ price: number; generatedAt: number; reason: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const priceDelta = usePriceDelta(symbol, ai?.decisionPrice ?? null);
  useEffect(() => {
    let alive = true;
    setError(null);
    dataSource
      .getAIAnalysis(symbol)
      .then((r) => {
        if (alive) setAi(r);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Failed to load signal");
      });
    dataSource
      .getSignals(12)
      .then((list) => {
        if (!alive) return;
        const s = list.find((x) => x.symbol === symbol) ?? list[0];
        if (s) setSig({ price: s.price, generatedAt: s.generatedAt, reason: s.reason });
      })
      .catch(() => {
        if (alive) setSig(null);
      });
    return () => {
      alive = false;
    };
  }, [symbol]);

  if (error) {
    return (
      <Panel title="Signal">
        <div className="empty">
          <div className="empty-icon" aria-hidden="true">⚠</div>
          <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Signal unavailable</div>
          <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{error}</div>
        </div>
      </Panel>
    );
  }

  if (!ai) return null;
  const confPct = Math.round((ai.confidence ?? 0) * 100);
  const signal = ai.signal ?? "no_signal";
  return (
    <Panel title="Signal" actions={<Badge kind={signal}>{signal.replace("_", " ")}</Badge>}>
      <div className="signal-emphasis">
        <span className={`signal-word sig-${signal}`}>{signal.replace("_", " ")}</span>
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
        {priceDelta !== null && ai?.decisionPrice != null && (
          <div className="stat">
            <span className="label">Live Δ</span>
            <span className={`value mono ${priceDelta > 0 ? "pos" : priceDelta < 0 ? "neg" : "muted"}`}>
              {priceDelta > 0 ? "+" : ""}₹{fmtPrice(priceDelta)}
            </span>
          </div>
        )}
      </div>
      <p className="faint" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        Analytical signal only. No order is placed — execution is disabled in this build.
      </p>
    </Panel>
  );
}
