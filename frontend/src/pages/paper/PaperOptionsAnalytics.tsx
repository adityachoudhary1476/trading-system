import { useCallback, useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import type {
  OptionExpiriesResponse,
  OptionGreeksRow,
  OptionsAnalyticsResponse,
} from "@/types/paper-api";
import {
  Panel,
  EmptyState,
  Button,
  MetricItem,
  Loading,
} from "@/components/ui";
import { DeploymentPicker } from "@/components/paper/paperShared";

/**
 * Candidate underlyings offered for selection. This is a shortlist of symbols
 * the paper stack is known to carry, not a claim that any particular one is
 * currently listed: if the exchange has nothing for the chosen symbol the
 * expiry panel says so rather than silently showing an empty chain.
 */
const UNDERLYING_CHOICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"];
const DEFAULT_UNDERLYING = UNDERLYING_CHOICES[0];
const WINDOW_OPTIONS = [0, 2, 5, 10, 25];

/**
 * Renders a greeks cell.
 *
 * null renders as an em dash, never 0.00. A zero delta and an
 * uncomputed delta are indistinguishable once rendered as a number, and only
 * one of them means "this position cannot move".
 */
function G({ value, digits = 4 }: { value: number | null; digits?: number }) {
  if (value === null || value === undefined) {
    return <span className="td-muted" title="not computable from available data">—</span>;
  }
  return <span style={{ fontFamily: "var(--mono)" }}>{value.toFixed(digits)}</span>;
}

function pct(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function num(value: number | null, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(digits);
}

export function PaperOptionsAnalytics() {
  const [deploymentId, setDeploymentId] = useState("");
  const [underlying, setUnderlying] = useState(DEFAULT_UNDERLYING);
  const [expiry, setExpiry] = useState("");
  const [windowStrikes, setWindowStrikes] = useState<number | undefined>(undefined);

  const [expiries, setExpiries] = useState<OptionExpiriesResponse | null>(null);
  const [expiriesLoading, setExpiriesLoading] = useState(false);
  const [expiriesError, setExpiriesError] = useState<string | null>(null);

  const [data, setData] = useState<OptionsAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Expires come from the exchange listing, not from a date arithmetic helper:
   * NSE weekly/monthly expiries do not follow a formula anyone can safely hard
   * code, and an invented date would 400 downstream. An empty result means the
   * provider could not answer, which is surfaced as such rather than as "no
   * expiries exist".
   */
  const loadExpiries = useCallback(async () => {
    if (!deploymentId) {
      setExpiries(null);
      setExpiriesError(null);
      return;
    }
    setExpiriesLoading(true);
    setExpiriesError(null);
    const res = await paperApi.getOptionExpiries(deploymentId, underlying);
    if (res.ok) {
      setExpiries(res.data);
      // Preselect the nearest expiry: it has the most time value and is the
      // one a reader is almost always asking about.
      setExpiry((current) =>
        current && res.data.expiries.includes(current) ? current : res.data.expiries[0] ?? "",
      );
    } else {
      setExpiries(null);
      setExpiry("");
      setExpiriesError(res.error.message);
    }
    setExpiriesLoading(false);
  }, [deploymentId, underlying]);

  useEffect(() => {
    loadExpiries();
  }, [loadExpiries]);

  const load = useCallback(async () => {
    if (!deploymentId || !expiry) {
      setData(null);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    const res = await paperApi.getOptionsAnalytics(deploymentId, underlying, {
      expiry,
      ...(windowStrikes !== undefined ? { strikes: windowStrikes } : {}),
    });
    if (res.ok) {
      setData(res.data);
    } else {
      setData(null);
      setError(res.error.message);
    }
    setLoading(false);
  }, [deploymentId, underlying, expiry, windowStrikes]);

  useEffect(() => {
    load();
  }, [load]);

  const summary = data?.summary;
  const hasRows = data !== null && data.rows.length > 0;

  return (
    <div className="paper-shell">
      <div className="pt-section">
        <h1>Options Analytics</h1>
        <span className="section-sub">
          Per-strike greeks across the chain — observational, no orders placed
        </span>
      </div>

      {/* Controls */}
      <Panel>
        <div className="row gap_md wrap">
          <div className="field">
            {/* The picker renders its own "Deployment" label. */}
            <DeploymentPicker
              value={deploymentId}
              onChange={setDeploymentId}
              placeholder="Select deployment."
            />
          </div>
          <div className="field">
            <label htmlFor="oa-underlying">Underlying</label>
            <select
              id="oa-underlying"
              value={underlying}
              onChange={(e) => setUnderlying(e.target.value)}
            >
              {UNDERLYING_CHOICES.map((u) => (
                <option key={u} value={u}>{u}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="oa-expiry">Expiry</label>
            <select
              id="oa-expiry"
              value={expiry}
              disabled={expiriesLoading || expiries?.expiries.length === 0}
              onChange={(e) => setExpiry(e.target.value)}
            >
              {!expiry && <option value="">Select expiry</option>}
              {(expiries?.expiries ?? []).map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="oa-window">Strikes either side of ATM</label>
            <select
              id="oa-window"
              value={windowStrikes === undefined ? "default" : String(windowStrikes)}
              onChange={(e) =>
                setWindowStrikes(
                  e.target.value === "default" ? undefined : Number(e.target.value),
                )
              }
            >
              <option value="default">Server default</option>
              {WINDOW_OPTIONS.map((n) => (
                <option key={n} value={String(n)}>
                  {n === 0 ? "ATM only" : `±${n}`}
                </option>
              ))}
            </select>
          </div>
        </div>
      </Panel>

      {/* Prerequisite state */}
      {!deploymentId && (
        <Panel subtle>
          <EmptyState
            title="Select a deployment"
            hint="Options analytics are scoped to a paper deployment."
          />
        </Panel>
      )}

      {deploymentId && expiriesLoading && (
        <Loading label="Loading expiries..." />
      )}

      {deploymentId && !expiriesLoading && expiriesError && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load expiries</div>
          <div className="es-hint">{expiriesError}</div>
          <Button variant="secondary" size="sm" onClick={loadExpiries}>
            Retry
          </Button>
        </div>
      )}

      {deploymentId && !expiriesLoading && !expiriesError && expiries && expiries.expiries.length === 0 && (
        <Panel subtle>
          <EmptyState
            title="No expiries available"
            hint={
              expiries.notes.length > 0
                ? expiries.notes.join(" ")
                : `The chain provider did not list any expiries for ${underlying}.`
            }
          />
        </Panel>
      )}

      {deploymentId && !expiriesLoading && !expiriesError && expiry && (
        <div className="td-muted">
          Listed {expiries?.count ?? 0} expiring{" "}
          {expiries?.count === 1 ? "contract" : "contracts"} for {underlying}.
        </div>
      )}

      {loading && <Loading label="Computing greeks..." />}

      {error && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load options analytics</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={load}>
            Retry
          </Button>
        </div>
      )}

      {/* ATM summary */}
      {!loading && !error && data && (
        <Panel title={`Summary — ${data.underlying} ${data.expiry}`}>
          <div className="metric-grid">
            <MetricItem
              label="ATM Strike"
              value={summary?.atm_strike !== null && summary?.atm_strike !== undefined
                ? summary.atm_strike.toFixed(0)
                : "—"}
            />
            <MetricItem
              label="Spot"
              value={data.spot_price !== null ? data.spot_price.toFixed(2) : "—"}
            />
            <MetricItem
              label="Net Delta"
              value={num(summary?.net_delta ?? null)}
            />
            <MetricItem label="Call Δ" value={num(summary?.call_delta_total ?? null)} />
            <MetricItem label="Put Δ" value={num(summary?.put_delta_total ?? null)} />
            <MetricItem label="Gamma" value={num(summary?.gamma_total ?? null, 6)} />
            <MetricItem label="Theta" value={num(summary?.theta_total ?? null, 4)} />
            <MetricItem label="Vega" value={num(summary?.vega_total ?? null, 4)} />
            <MetricItem label="Put/Call OI" value={num(summary?.put_call_oi_ratio ?? null)} />
          </div>

          {/* Coverage is shown next to the totals, never buried: a net delta of
              120 computed from 6 of 40 rows means something quite different
              from one computed from all 40. */}
          <div className="mt_sm td-muted">
            {summary
              ? `Greeks available for ${summary.rows_with_greeks} of ${summary.rows_total} rows (${pct(summary.coverage)})`
              : ""}
            {data.window_truncated && " — window clamped to the maximum"}
          </div>
        </Panel>
      )}

      {/* Notes */}
      {!loading && !error && data && data.notes.length > 0 && (
        <Panel title="Notes" className="mt_md">
          <ul>
            {data.notes.map((n) => (
              <li key={n} className="td-muted">{n}</li>
            ))}
          </ul>
        </Panel>
      )}

      {/* Per-strike table */}
      {!loading && !error && data && (
        <Panel title="Greeks by strike" className="mt_md">
          {hasRows ? (
            <table className="data dense">
              <thead>
                <tr>
                  <th>Strike</th>
                  <th>Side</th>
                  <th>Moneyness</th>
                  <th>LTP</th>
                  <th>Bid</th>
                  <th>Ask</th>
                  <th>OI</th>
                  <th>Chg OI</th>
                  <th>IV</th>
                  <th>IV Src</th>
                  <th>Delta</th>
                  <th>Gamma</th>
                  <th>Theta</th>
                  <th>Vega</th>
                  <th>Rho</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r: OptionGreeksRow) => (
                  <tr key={`${r.strike}-${r.option_type}`}>
                    <td style={{ fontFamily: "var(--mono)" }}>{r.strike.toFixed(0)}</td>
                    <td><span className="pill">{r.option_type}</span></td>
                    <td>{r.moneyness ?? "—"}</td>
                    <td>{num(r.ltp)}</td>
                    <td>{num(r.bid)}</td>
                    <td>{num(r.ask)}</td>
                    <td>{r.oi ?? "—"}</td>
                    <td>{r.change_oi ?? "—"}</td>
                    <td>{pct(r.implied_vol)}</td>
                    <td className="td-muted">{r.iv_source ?? "—"}</td>
                    <td><G value={r.delta} /></td>
                    <td><G value={r.gamma} digits={6} /></td>
                    <td><G value={r.theta} digits={4} /></td>
                    <td><G value={r.vega} digits={4} /></td>
                    <td><G value={r.rho} digits={4} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No greeks available"
              hint={
                data.notes.length > 0
                  ? data.notes.join(" ")
                  : "No chain rows were returned for this expiry."
              }
            />
          )}
        </Panel>
      )}

      <div className="pt-section">
        <span className="section-sub">
          OBSERVATIONAL — this view never places, registers, or enables orders.
        </span>
      </div>
    </div>
  );
}