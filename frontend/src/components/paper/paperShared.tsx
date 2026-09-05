import { useEffect, useState, useCallback } from "react";
import { paperApi } from "@/lib/paperApi";
import type { DeploymentListResponse } from "@/types/paper-api";
import { Loading, Button } from "@/components/ui";

/** Shared deployment picker dropdown used across paper-trading index pages. */
export function DeploymentPicker({
  value,
  onChange,
  placeholder = "Select deployment…",
  onCreateDeployment,
  refreshKey,
}: {
  value: string;
  onChange: (id: string) => void;
  placeholder?: string;
  /** Called when the user picks "Create" from an empty/error state. */
  onCreateDeployment?: () => void;
  /** Bump to force a reload (e.g. after a sibling created a deployment). */
  refreshKey?: unknown;
}) {
  const [list, setList] = useState<DeploymentListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    paperApi.listDeployments({ limit: 200 }).then((res) => {
      if (!alive) return;
      if (res.ok) {
        setList(res.data);
      } else {
        setList(null);
        setError(res.error.message);
      }
      setLoading(false);
    });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    return load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, refreshKey]);

  if (loading) return <Loading label="Loading deployments…" />;

  if (error) {
    return (
      <div className="deploy-picker dp-error" role="alert">
        <span className="dp-label">Deployment</span>
        <div className="dp-error-block">
          <span className="dp-error-msg">Unable to load deployments</span>
          <span className="dp-error-hint">{error}</span>
          <Button variant="secondary" size="sm" onClick={load}>Retry</Button>
          {onCreateDeployment && (
            <Button variant="primary" size="sm" onClick={onCreateDeployment}>Create deployment</Button>
          )}
        </div>
      </div>
    );
  }

  const empty = list?.deployments.length === 0;

  return (
    <div className="deploy-picker">
      <span className="dp-label">Deployment</span>
      {empty && (
        <div className="dp-empty">
          <span className="dp-empty-hint">No paper deployments yet</span>
          {onCreateDeployment && (
            <Button variant="primary" size="sm" onClick={onCreateDeployment}>Create deployment</Button>
          )}
        </div>
      )}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Select deployment"
        style={empty ? { display: "none" } : undefined}
      >
        <option value="">{placeholder}</option>
        {list?.deployments.map((d) => (
          <option key={d.deployment_id} value={d.deployment_id}>
            {d.deployment_id} — {d.symbol} · {d.timeframe} [{d.status}]
          </option>
        ))}
      </select>
      {value && list && (() => {
        const selected = list.deployments.find((d) => d.deployment_id === value);
        if (!selected) return null;
        return (
          <span className="dp-meta">
            {selected.symbol} · {selected.timeframe} [{selected.status}]
          </span>
        );
      })()}
    </div>
  );
}

/** Evidence timeline showing research → walk-forward → paper progression. */
export function EvidenceTimeline({
  researchCount,
  walkForwardCount,
  paperCount,
}: {
  researchCount: number;
  walkForwardCount: number;
  paperCount: number;
}) {
  return (
    <div className="evidence-timeline" aria-label="Strategy evidence progression">
      <div className={`evidence-node${researchCount > 0 ? " has-data" : ""}`}>
        <div className="en-dot" />
        <span className="en-label">Research</span>
        <span className="en-count">{researchCount}</span>
      </div>
      <div className="evidence-connector" />
      <div className={`evidence-node${walkForwardCount > 0 ? " has-data" : ""}`}>
        <div className="en-dot" />
        <span className="en-label">Walk Forward</span>
        <span className="en-count">{walkForwardCount}</span>
      </div>
      <div className="evidence-connector" />
      <div className={`evidence-node${paperCount > 0 ? " has-data" : ""}`}>
        <div className="en-dot" />
        <span className="en-label">Paper Trading</span>
        <span className="en-count">{paperCount}</span>
      </div>
    </div>
  );
}

/** Format helpers shared across paper-trading pages. */
export const fmt = {
  currency: (v: number | null | undefined, digits = 2): string => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    return `₹${v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  },
  currencyPlain: (v: number | null | undefined, digits = 2): string => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    return v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  pct: (v: number | null | undefined): string => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
  },
  pctPlain: (v: number | null | undefined): string => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    return `${(v * 100).toFixed(2)}%`;
  },
  num: (v: number | null | undefined): string => {
    if (v === null || v === undefined || !Number.isFinite(v)) return "—";
    return v.toLocaleString("en-IN");
  },
};
