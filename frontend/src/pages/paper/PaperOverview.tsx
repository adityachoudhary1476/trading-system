import { useEffect, useState, useMemo, useCallback } from "react";
import { Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type {
  DashboardSnapshotResponse,
  SessionStatus,
  HealthStatus,
  RiskDecision,
} from "@/types/paper-api";
import { EmptyState, Button, MetricItem } from "@/components/ui";
import { DeploymentPicker, fmt } from "@/components/paper/paperShared";

export function PaperOverview() {
  const [deploymentId, setDeploymentId] = useState("");
  const [snapshot, setSnapshot] = useState<DashboardSnapshotResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (id: string) => {
    if (!id) return;
    setLoading(true);
    setError(null);
    const res = await paperApi.getDashboard(id);
    if (res.ok) {
      setSnapshot(res.data);
    } else {
      setError(res.error.message);
      setSnapshot(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (deploymentId) fetchData(deploymentId);
  }, [deploymentId, fetchData]);

  return (
    <div className="paper-shell">
      <DeploymentPicker value={deploymentId} onChange={setDeploymentId} />

      {!deploymentId && !loading && (
        <EmptyState
          title="No paper deployment selected"
          hint="Select a deployment above to inspect its terminal state."
        />
      )}

      {loading && deploymentId && <TerminalSkeleton />}

      {error && deploymentId && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load paper terminal</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={() => fetchData(deploymentId)}>Retry</Button>
        </div>
      )}

      {snapshot && !loading && (
        <TerminalView
          data={snapshot}
          onRefresh={() => fetchData(deploymentId)}
        />
      )}
    </div>
  );
}

function TerminalSkeleton() {
  return (
    <div className="stack gap-lg" aria-label="Loading terminal">
      <div className="skel skel-block" style={{ height: 110 }} />
      <div className="skel skel-block" style={{ height: 78 }} />
      <div className="tl-split">
        <div className="skel skel-block" style={{ height: 280 }} />
        <div className="skel skel-block" style={{ height: 280 }} />
      </div>
      <div className="skel skel-block" style={{ height: 160 }} />
      <div className="skel skel-block" style={{ height: 160 }} />
      <div className="skel skel-block" style={{ height: 200 }} />
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* Terminal view                                                           */
/* ---------------------------------------------------------------------- */

function TerminalView({
  data,
  onRefresh,
}: {
  data: DashboardSnapshotResponse;
  onRefresh: () => void;
}) {
  const isHalted = data.health.status === "halted";
  const isCircuitOpen = data.circuit_breaker.state === "open";
  const isFailed = data.deployment.status === "failed";
  const isCritical = isHalted || isCircuitOpen || isFailed;

  return (
    <div className="stack gap-lg">
      <TerminalKPIs data={data} onRefresh={onRefresh} />

      {isCritical && <CriticalBanner data={data} />}

      <div className="tl-split">
        <EquityPanel data={data} />
        <SystemStatusPanel data={data} />
      </div>

      <PositionsPanel data={data} />
      <ActivityTimeline data={data} />
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* KPI strip                                                                */
/* ---------------------------------------------------------------------- */

function TerminalKPIs({
  data,
  onRefresh,
}: {
  data: DashboardSnapshotResponse;
  onRefresh: () => void;
}) {
  const acct = data.account;
  const perf = data.performance;
  const sess = data.session;

  const startCap = acct.starting_equity ?? acct.initial_cash ?? null;
  const equity = acct.equity;
  const cash = acct.available_cash;
  const todayPnl = acct.unrealized_pnl;
  const totalPnl =
    perf.total_pnl ?? (acct.realized_pnl != null && acct.unrealized_pnl != null
      ? acct.realized_pnl + acct.unrealized_pnl
      : acct.realized_pnl);
  const positions = data.positions.is_flat ? 0 : 1;
  const trades = perf.trade_count ?? 0;
  const drawdown = perf.drawdown;

  const equityFillPct = useMemo(() => {
    if (startCap == null || equity == null || startCap <= 0) return 0;
    const pct = Math.max(0, Math.min(1, equity / startCap));
    return pct;
  }, [startCap, equity]);

  const drawdownPct = useMemo(() => {
    if (drawdown == null) return 0;
    return Math.max(0, Math.min(1, Math.abs(drawdown)));
  }, [drawdown]);

  return (
    <div className="tl-panel">
      <div className="tl-panel-head">
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          Account &amp; Performance Telemetry
        </div>
        <div className="tl-panel-actions">
          <Button variant="ghost" size="xs" onClick={onRefresh}>Refresh</Button>
        </div>
      </div>
      <div className="tl-kpis" role="list">
        <KpiCell
          label="Starting Capital"
          value={fmt.currency(startCap)}
          tone="muted"
        />
        <KpiCell
          label="Current Equity"
          value={fmt.currency(equity)}
          tone="accent"
          sub={startCap != null ? `vs ${fmt.currency(startCap)}` : undefined}
          bar={equityFillPct}
        />
        <KpiCell
          label="Available Cash"
          value={fmt.currency(cash)}
          tone="muted"
        />
        <KpiCell
          label="Today's P&L"
          value={fmt.currency(todayPnl)}
          tone={toneForPnl(todayPnl)}
        />
        <KpiCell
          label="Total P&L"
          value={fmt.currency(totalPnl)}
          tone={toneForPnl(totalPnl)}
          sub={fmt.pct(perf.return)}
        />
        <KpiCell
          label="Open Positions"
          value={String(positions)}
          tone={positions > 0 ? "accent" : "muted"}
          sub={data.positions.is_flat ? "Flat" : data.positions.open_position?.symbol ?? undefined}
        />
        <KpiCell
          label="Trades"
          value={String(trades)}
          tone="muted"
          sub={`${fmt.num(sess.orders_submitted ?? perf.orders_submitted)} orders`}
        />
        <KpiCell
          label="Drawdown"
          value={fmt.pctPlain(drawdown)}
          tone="neg"
          bar={drawdownPct}
        />
      </div>
    </div>
  );
}

function KpiCell({
  label,
  value,
  sub,
  tone,
  bar,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg" | "warn" | "accent" | "muted";
  bar?: number;
}) {
  return (
    <div className={`tl-kpi${tone ? ` ${tone}` : ""}`} role="listitem">
      <div className="tl-k">{label}</div>
      <div className="tl-v">{value}</div>
      {sub && <div className="tl-s">{sub}</div>}
      {bar != null && bar > 0 && (
        <div className="tl-bar" aria-hidden="true">
          <span style={{ width: `${Math.round(bar * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* Critical banner                                                          */
/* ---------------------------------------------------------------------- */

function CriticalBanner({ data }: { data: DashboardSnapshotResponse }) {
  const isHalted = data.health.status === "halted";
  const isCircuitOpen = data.circuit_breaker.state === "open";
  const isFailed = data.deployment.status === "failed";
  let title = "CRITICAL STATE";
  let sub: string | null = null;
  if (isHalted) {
    title = "Paper Execution Halted";
    sub = data.health.halt_reason || "Paper execution is currently blocked.";
  } else if (isCircuitOpen) {
    title = "Circuit Breaker Open";
    sub = data.circuit_breaker.reason || "Trading halted by circuit breaker.";
  } else if (isFailed) {
    title = "Deployment Failed";
    sub = "Deployment has failed and lifecycle actions are unavailable.";
  }
  return (
    <div className="tl-banner" role="alert">
      <div className="tlb-icon" aria-hidden="true">!</div>
      <div className="tlb-text">
        <div className="tlb-title">{title}</div>
        {sub && <div className="tlb-sub">{sub}</div>}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* Equity curve panel                                                       */
/* ---------------------------------------------------------------------- */

function EquityPanel({ data }: { data: DashboardSnapshotResponse }) {
  const acct = data.account;
  const perf = data.performance;
  const startCap = acct.starting_equity ?? acct.initial_cash ?? null;
  const equity = acct.equity;
  const totalPnl =
    perf.total_pnl ?? (acct.realized_pnl != null && acct.unrealized_pnl != null
      ? acct.realized_pnl + acct.unrealized_pnl
      : acct.realized_pnl);

  return (
    <section className="tl-panel tl-equity" aria-label="Equity Curve">
      <div className="tl-panel-head">
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          Equity Curve
        </div>
        <div className="tl-panel-actions">
          <span>Current</span>
          <span className="pos mono">{fmt.currency(equity)}</span>
        </div>
      </div>
      <div className="tl-equity-chart">
        <div className="tl-equity-empty" role="status">
          <div className="tle-title">No Performance History</div>
          <div className="tle-sub">Waiting for session data…</div>
        </div>
      </div>
      <div className="tl-panel-head" style={{ borderTop: "1px solid var(--panel-border-soft)", borderBottom: "none" }}>
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          Session P&amp;L Breakdown
        </div>
        <div className="tl-panel-actions">
          <span>Total</span>
          <span className={`mono ${toneForPnl(totalPnl)}`}>{fmt.currency(totalPnl)}</span>
        </div>
      </div>
      <div className="metric-grid" style={{ padding: 12 }}>
        <MetricItem label="Starting Equity" value={fmt.currency(startCap)} />
        <MetricItem
          label="Realized"
          value={fmt.currency(acct.realized_pnl)}
          tone={toneForPnl(acct.realized_pnl)}
        />
        <MetricItem
          label="Unrealized"
          value={fmt.currency(acct.unrealized_pnl)}
          tone={toneForPnl(acct.unrealized_pnl)}
        />
        <MetricItem
          label="Return"
          value={fmt.pct(perf.return)}
          tone={toneForPnl(perf.return)}
        />
        <MetricItem
          label="Drawdown"
          value={fmt.pctPlain(perf.drawdown)}
          tone="neg"
        />
        <MetricItem
          label="Exposure"
          value={fmt.pct(perf.exposure)}
          tone="muted"
        />
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------------- */
/* System status panel                                                      */
/* ---------------------------------------------------------------------- */

function SystemStatusPanel({ data }: { data: DashboardSnapshotResponse }) {
  const sess = data.session;
  const cb = data.circuit_breaker;
  const risk = data.risk;
  const health = data.health;

  const sessionTone = mapSessionTone(sess.session_status);
  const deploymentTone = mapSessionTone(sess.deployment_status);
  const healthTone = mapHealthTone(health.status);
  const riskTone = mapRiskTone(risk.decision);
  const cbTone = cb.state === "open" ? "is-critical" : "is-healthy";
  const brokerTone = sessionTone === "is-critical" ? "is-warning" : "is-active";
  const strategyTone = sessionTone === "is-critical" ? "is-warning" : "is-active";
  const dataTone = sessionTone === "is-critical" ? "is-warning" : "is-active";

  const lastBar = sess.last_processed_bar_timestamp;

  return (
    <section className="tl-panel tl-system" aria-label="System Status">
      <div className="tl-panel-head">
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          System Status
        </div>
        <div className="tl-panel-actions">
          <span>Command Center</span>
        </div>
      </div>
      <div>
        <SystemRow
          label="Market Data"
          state={mapStateLabel(dataTone)}
          tone={dataTone}
        />
        <SystemRow
          label="Strategy"
          state={mapStateLabel(strategyTone)}
          tone={strategyTone}
        />
        <SystemRow
          label="Risk Engine"
          state={risk.decision.toUpperCase()}
          tone={riskTone}
          hint={risk.reason}
        />
        <SystemRow
          label="Paper Broker"
          state={mapStateLabel(brokerTone)}
          tone={brokerTone}
        />
        <SystemRow
          label="Session"
          state={sess.session_status.toUpperCase()}
          tone={sessionTone}
        />
        <SystemRow
          label="Circuit Breaker"
          state={cb.state.toUpperCase()}
          tone={cbTone}
          hint={cb.reason}
        />
        <SystemRow
          label="Health"
          state={health.status.toUpperCase()}
          tone={healthTone}
          hint={health.halt_reason ?? (health.warnings.length ? health.warnings.join(" · ") : null)}
        />
        <SystemRow
          label="Deployment"
          state={sess.deployment_status.toUpperCase()}
          tone={deploymentTone}
        />
      </div>
      <div className="tl-system-foot">
        <span>Last Bar</span>
        <span className="mono">
          {lastBar ? formatTs(lastBar) : "—"}
        </span>
      </div>
    </section>
  );
}

function SystemRow({
  label,
  state,
  tone,
  hint,
}: {
  label: string;
  state: string;
  tone: string;
  hint?: string | null;
}) {
  return (
    <>
      <div className={`tl-system-row ${tone}`}>
        <div className="tlsr-label">
          <span className="tlsr-dot" aria-hidden="true" />
          <span>{label}</span>
        </div>
        <div className="tlsr-state">{state}</div>
      </div>
      {hint && (
        <div
          style={{
            padding: "4px 14px 8px",
            fontFamily: "var(--mono)",
            fontSize: 10.5,
            color: "var(--text-faint)",
            borderBottom: "1px solid var(--panel-border-soft)",
            letterSpacing: 0.3,
          }}
        >
          {hint}
        </div>
      )}
    </>
  );
}

function mapSessionTone(s: SessionStatus): string {
  if (s === "active" || s === "restored") return "is-active";
  if (s === "paused" || s === "checkpointed") return "is-warning";
  if (s === "failed" || s === "stopped") return "is-critical";
  return "is-healthy";
}

function mapHealthTone(s: HealthStatus): string {
  if (s === "healthy") return "is-healthy";
  if (s === "warning") return "is-warning";
  if (s === "halted") return "is-critical";
  return "is-warning";
}

function mapRiskTone(d: RiskDecision): string {
  if (d === "allow") return "is-healthy";
  if (d === "warning") return "is-warning";
  if (d === "halt") return "is-critical";
  return "is-warning";
}

function mapStateLabel(tone: string): string {
  if (tone === "is-active") return "ACTIVE";
  if (tone === "is-warning") return "WARNING";
  if (tone === "is-critical") return "HALTED";
  return "READY";
}

/* ---------------------------------------------------------------------- */
/* Positions panel                                                          */
/* ---------------------------------------------------------------------- */

function PositionsPanel({ data }: { data: DashboardSnapshotResponse }) {
  const dep = data.deployment;
  const open = data.positions.open_position;
  const isFlat = data.positions.is_flat || !open;

  return (
    <section className="tl-panel" aria-label="Open Positions">
      <div className="tl-panel-head">
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          Open Positions
        </div>
        <div className="tl-panel-actions">
          <Link
            to={`/paper/positions/${dep.deployment_id}`}
            className="section-sub"
          >
            Details ›
          </Link>
        </div>
      </div>
      {isFlat ? (
        <div className="tl-empty" role="status">
          <div className="tle-title">No Open Positions</div>
          <div className="tle-sub">
            The paper portfolio currently has no open positions.
          </div>
        </div>
      ) : (
        <div className="tl-table-wrap">
          <table className="tl-table" aria-label="Open positions">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th className="num">Qty</th>
                <th className="num">Avg Entry</th>
                <th className="num">LTP</th>
                <th className="num">Market Value</th>
                <th className="num">Unrealized P&amp;L</th>
                <th className="num">Return</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{open!.symbol}</td>
                <td>
                  <span className={`tl-side ${open!.side === "long" ? "long" : "short"}`}>
                    {open!.side.toUpperCase()}
                  </span>
                </td>
                <td className="num">{open!.quantity.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</td>
                <td className="num">{fmt.currency(open!.entry_price)}</td>
                <td className="num">{fmt.currency(open!.current_price)}</td>
                <td className="num">{fmt.currency(open!.position_value)}</td>
                <td className={`num ${open!.unrealized_pnl >= 0 ? "pos" : "neg"}`}>
                  {fmt.currency(open!.unrealized_pnl)}
                </td>
                <td className={`num ${open!.unrealized_pnl >= 0 ? "pos" : "neg"}`}>
                  {fmt.pct(
                    open!.entry_price > 0
                      ? (open!.current_price - open!.entry_price) / open!.entry_price
                      : null,
                  )}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------------- */
/* Activity timeline                                                        */
/* ---------------------------------------------------------------------- */

function ActivityTimeline({ data }: { data: DashboardSnapshotResponse }) {
  const recent = data.recent_events;
  const reversed = recent.recent ? [...recent.recent].reverse() : [];

  return (
    <section className="tl-panel" aria-label="Decision Timeline">
      <div className="tl-panel-head">
        <div className="tl-panel-title">
          <span className="tl-bar2" aria-hidden="true" />
          Decision Timeline
        </div>
        <div className="tl-panel-actions">
          <span>
            {recent.total_events ?? 0} events · last #{recent.last_event_sequence ?? "—"}
          </span>
          <Link
            to={`/paper/events/${data.deployment.deployment_id}`}
            className="section-sub"
          >
            View all ›
          </Link>
        </div>
      </div>
      <div className="tl-panel-body">
        {reversed.length === 0 ? (
          <div className="tl-empty">
            <div className="tle-title">No Activity Yet</div>
            <div className="tle-sub">
              Decision events will appear here as the deployment processes data.
            </div>
          </div>
        ) : (
          <div className="tl-timeline">
            {reversed.slice(0, 12).map((ev) => {
              const tone = toneForEvent(ev.event_type);
              const time = ev.timestamp ? ev.timestamp.slice(11, 19) : "—";
              return (
                <div key={ev.sequence} className={`tl-timeline-row ${tone}`}>
                  <div className="tltr-time">{time}</div>
                  <div className="tltr-axis" aria-hidden="true" />
                  <div className="tltr-body">
                    <div className="tltr-head">
                      <span className="tltr-type">
                        {ev.event_type.replace(/_/g, " ")}
                      </span>
                      <span className="faint mono" style={{ fontSize: 10 }}>
                        #{ev.sequence}
                      </span>
                    </div>
                    <div className="tltr-detail">{ev.message || "—"}</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------------- */
/* Helpers                                                                  */
/* ---------------------------------------------------------------------- */

function toneForPnl(v: number | null | undefined): "pos" | "neg" | "muted" {
  if (v === null || v === undefined) return "muted";
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "muted";
}

function toneForEvent(type: string): string {
  if (
    type === "order_rejected" ||
    type === "health_warning" ||
    type === "circuit_breaker_tripped" ||
    type === "deployment_stopped"
  )
    return "tone-neg";
  if (
    type === "circuit_breaker_reset" ||
    type === "session_restored" ||
    type === "deployment_paused" ||
    type === "deployment_resumed"
  )
    return "tone-warn";
  if (type === "fill_received") return "tone-pos";
  if (type === "signal_generated") return "tone-pos";
  return "tone-muted";
}

function formatTs(ts: string): string {
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleString();
  } catch {
    return ts;
  }
}