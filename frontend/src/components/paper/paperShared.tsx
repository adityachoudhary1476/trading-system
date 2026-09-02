import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import type { DeploymentListResponse } from "@/types/paper-api";
import { Loading } from "@/components/ui";

/** Shared deployment picker dropdown used across paper-trading index pages. */
export function DeploymentPicker({
  value,
  onChange,
  placeholder = "Select deployment…",
}: {
  value: string;
  onChange: (id: string) => void;
  placeholder?: string;
}) {
  const [list, setList] = useState<DeploymentListResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    paperApi.listDeployments({ limit: 200 }).then((res) => {
      if (!alive) return;
      if (res.ok) setList(res.data);
      setLoading(false);
    });
    return () => { alive = false; };
  }, []);

  if (loading) return <Loading label="Loading deployments…" />;

  return (
    <div className="deploy-picker">
      <span className="dp-label">Deployment</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Select deployment">
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
