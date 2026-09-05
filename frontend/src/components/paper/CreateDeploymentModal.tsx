import { useState, useCallback } from "react";
import { paperApi } from "@/lib/paperApi";
import { Button } from "@/components/ui";

export const PRESET_STRATEGIES = [
  {
    key: "sma5",
    label: "SMA(5) Trend Following",
    description: "Buy when close > 5-bar SMA",
    spec: (symbol: string, timeframe: string, name: string) => ({
      name,
      description: "Buy when close exceeds the 5-period simple moving average.",
      symbol,
      timeframe,
      indicators: [{ name: "sma", params: { window: 5 } }],
      entry: {
        type: "comparison" as const,
        left: { kind: "field" as const, field: "close" },
        op: ">",
        right: { kind: "indicator" as const, indicator: "sma_5" },
      },
      allow_long: true,
      generated_by: "paper-ui",
    }),
  },
  {
    key: "sma20_50",
    label: "SMA(20/50) Crossover",
    description: "Buy when 20-bar SMA crosses above 50-bar SMA",
    spec: (symbol: string, timeframe: string, name: string) => ({
      name,
      description: "Trend-following: enter on SMA crossover.",
      symbol,
      timeframe,
      indicators: [
        { name: "sma", params: { window: 20 } },
        { name: "sma", params: { window: 50 } },
      ],
      entry: {
        type: "comparison" as const,
        left: { kind: "indicator" as const, indicator: "sma_20" },
        op: "crosses_above",
        right: { kind: "indicator" as const, indicator: "sma_50" },
      },
      allow_long: true,
      generated_by: "paper-ui",
    }),
  },
  {
    key: "rsi14",
    label: "RSI(14) Mean Reversion",
    description: "Buy when RSI(14) < 30 and close > SMA(20)",
    spec: (symbol: string, timeframe: string, name: string) => ({
      name,
      description: "Mean-reversion: buy when RSI is oversold and price is above SMA.",
      symbol,
      timeframe,
      indicators: [
        { name: "sma", params: { window: 20 } },
        { name: "rsi", params: { window: 14 } },
      ],
      entry: {
        type: "logic" as const,
        op: "AND",
        conditions: [
          {
            type: "comparison" as const,
            left: { kind: "field" as const, field: "close" },
            op: ">",
            right: { kind: "indicator" as const, indicator: "sma_20" },
          },
          {
            type: "comparison" as const,
            left: { kind: "indicator" as const, indicator: "rsi_14" },
            op: "<",
            right: { kind: "constant" as const, constant: 30 },
          },
        ],
      },
      allow_long: true,
      generated_by: "paper-ui",
    }),
  },
] as const;

export function CreateDeploymentModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (deploymentId: string) => void;
}) {
  const [preset, setPreset] = useState("sma5");
  const [name, setName] = useState("My Paper Deployment");
  const [symbol, setSymbol] = useState("NSE:SBIN");
  const [timeframe, setTimeframe] = useState("1d");
  const [initialCash, setInitialCash] = useState("100000");
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const presetObj = PRESET_STRATEGIES.find((p) => p.key === preset) ?? PRESET_STRATEGIES[0];
  const spec = presetObj.spec(symbol, timeframe, name);

  const handleSubmit = useCallback(async () => {
    setSubmitError(null);
    const cash = parseFloat(initialCash);
    if (isNaN(cash) || cash <= 0) {
      setSubmitError("Initial capital must be a positive number.");
      return;
    }
    setBusy(true);
    const res = await paperApi.createDeployment({
      spec: spec as Record<string, unknown>,
      config: { initial_cash: cash },
    });
    setBusy(false);
    if (res.ok) {
      setName("My Paper Deployment");
      setSymbol("NSE:SBIN");
      setTimeframe("1d");
      setInitialCash("100000");
      onCreated(res.data.deployment.deployment_id);
    } else {
      setSubmitError(res.error.message);
    }
  }, [spec, initialCash, onCreated]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose} role="presentation">
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head" id="modal-title">Create Deployment</div>
        <div className="modal-body">
          <div className="form-group">
            <label htmlFor="deploy-name">Deployment Name</label>
            <input id="deploy-name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label htmlFor="deploy-symbol">Symbol</label>
              <input id="deploy-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} disabled={busy} />
            </div>
            <div className="form-group">
              <label htmlFor="deploy-timeframe">Timeframe</label>
              <input id="deploy-timeframe" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} disabled={busy} />
            </div>
          </div>
          <div className="form-group">
            <label htmlFor="deploy-capital">Initial Capital</label>
            <input id="deploy-capital" type="number" min="1" step="1000" value={initialCash} onChange={(e) => setInitialCash(e.target.value)} disabled={busy} />
          </div>
          <div className="form-group">
            <label htmlFor="deploy-strategy">Strategy Preset</label>
            <select id="deploy-strategy" value={preset} onChange={(e) => setPreset(e.target.value)} disabled={busy}>
              {PRESET_STRATEGIES.map((p) => (
                <option key={p.key} value={p.key}>{p.label} — {p.description}</option>
              ))}
            </select>
          </div>
          {submitError && (
            <div className="fb-error feedback" role="alert">{submitError}</div>
          )}
        </div>
        <div className="modal-foot">
          <Button variant="secondary" size="sm" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" size="sm" onClick={handleSubmit} disabled={busy}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </div>
    </div>
  );
}
