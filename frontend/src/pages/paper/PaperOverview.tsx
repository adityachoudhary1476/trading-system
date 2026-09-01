import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { DashboardSnapshotResponse } from "@/types/paper-api";
import { Panel, EmptyState, Button, StatusIndicator, MetricItem } from "@/components/ui";
import { DeploymentPicker, fmt } from "@/components/paper/paperShared";

export function PaperOverview() {
  const [deploymentId, setDeploymentId] = useState("");
  const [snapshot, setSnapshot] = useState<DashboardSnapshotResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async (id: string) => {
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
  };

  useEffect(() => {
    if (deploymentId) fetchData(deploymentId);
  }, [deploymentId]);

  return (
    <div className="paper-shell">
      <DeploymentPicker value={deploymentId} onChange={setDeploymentId} />

      {!deploymentId && !loading && (
        <EmptyState
          title="No paper deployment selected"
          hint="Select a deployment above to inspect its performance and operational state."
        />
      )}

      {loading && deploymentId && <LoadingSkeleton />}

      {error && deploymentId && !loading && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load paper overview</div>
          <div className="es-hint">{error}</div>
          <Button variant="secondary" size="sm" onClick={() => fetchData(deploymentId)}>Retry</Button>
        </div>
      )}

      {snapshot && !loading && <SnapshotView data={snapshot} onRefresh={() => fetchData(deploymentId)} />}
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="stack gap-lg" aria-label="Loading dashboard">
      <div className="skel skel-block" style={{ height: 110 }} />
      <div className="kpi-row">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skel skel-block" style={{ height: 78 }} />
        ))}
      </div>
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div className="skel skel-block" style={{ height: 180 }} />
        <div className="skel skel-block" style={{ height: 180 }} />
      </div>
      <div className="skel skel-block" style={{ height: 160 }} />
      <div className="skel skel-block" style={{ height: 180 }} />
    </div>
  );
}

function SnapshotView({ data, onRefresh }: { data: DashboardSnapshotResponse; onRefresh: () => void }) {
  const isHalted = data.health.status === "halted";
  const isFailed = data.deployment.status === "failed";
  const isCircuitOpen = data.circuit_breaker.state === "open";
  const isCritical = isHalted || isFailed || isCircuitOpen;

  return (
    <div className="stack gap-lg">
      {/* Deployment Context Header */}
      <div className={`detail-head${isCritical ? " circuit-open" : ""}`}>
        <div className="dh-top">
          <div>
            <div className="dh-title">
              {data.deployment.symbol} · {data.deployment.timeframe}
            </div>
            <div className="dh-sub">
              {data.strategy?.name ?? data.deployment.strategy_id}
            </div>
          </div>
          <div className="row gap-sm">
            <StatusIndicator status={data.deployment.status} />
            <span className="pill">{data.deployment.execution_mode.toUpperCase()}</span>
          </div>
        </div>
        <div className="dh-metrics">
          <div className="dh-metric">
            <span className="m-label">Session</span>
            <span className="m-value">{data.session.session_status}</span>
          </div>
          <div className="dh-metric">
            <span className="m-label">Bars</span>
            <span className="m-value">{fmt.num(data.performance.bar_count)}</span>
          </div>
          <div className="dh-metric">
            <span className="m-label">Return</span>
            <span className="m-value">{fmt.pct(data.performance.return)}</span>
          </div>
        </div>
      </div>

      {/* Critical State Banner */}
      {isCritical && (
        <div className="risk-block rb-critical" role="alert">
          <div className="rb-top">
            <StatusIndicator status={isHalted ? "halted" : isFailed ? "failed" : "open"} />
            <span className="rb-status">
              {isHalted ? "HALTED" : isFailed ? "FAILED" : "CIRCUIT OPEN"}
            </span>
          </div>
          <div className="rb-reason">
            {isHalted && (data.health.halt_reason || "Paper execution is currently blocked.")}
            {isFailed && "Deployment has failed."}
            {isCircuitOpen && (data.circuit_breaker.reason || "Circuit breaker is open.")}
          </div>
        </div>
      )}

      {/* KPI Row */}
      <div>
        <div className="pt-section">
          <h2>Key Metrics</h2>
          <Button variant="ghost" size="xs" onClick={onRefresh}>Refresh</Button>
        </div>
        <div className="kpi-row">
          <KpiCard
            label="Equity"
            value={fmt.currency(data.account.equity)}
            sub={`Start ${fmt.currency(data.account.starting_equity)}`}
            tone="accent"
          />
          <KpiCard
            label="Realized P&L"
            value={fmt.currency(data.account.realized_pnl)}
            sub={fmt.pct(data.performance.return)}
            tone={toneForPnl(data.account.realized_pnl)}
          />
          <KpiCard
            label="Return"
            value={fmt.pct(data.performance.return)}
            tone={toneForPnl(data.performance.return)}
          />
          <KpiCard
            label="Drawdown"
            value={fmt.pctPlain(data.performance.drawdown)}
            tone="neg"
          />
          <KpiCard
            label="Positions"
            value={data.positions.is_flat ? "0" : "1"}
            sub={data.positions.is_flat ? "Flat" : data.positions.open_position?.symbol}
            tone="muted"
          />
          <KpiCard
            label="Orders"
            value={fmt.num(data.performance.orders_submitted)}
            sub={`${data.performance.fills_received} fills`}
            tone="muted"
          />
        </div>
      </div>

      {/* Performance History - Empty State (no historical data) */}
      <div>
        <div className="pt-section"><h2>Performance History</h2></div>
        <Panel subtle>
          <EmptyState
            title="Historical performance data is not available"
            hint="Live performance history will appear here once time-series data is available."
          />
        </Panel>
      </div>

      {/* Operations + Risk & Health */}
      <div className="grid cols-2" style={{ gap: 12 }}>
        <Panel title="Operations">
          <div className="metric-grid">
            <MetricItem label="Bars Processed" value={fmt.num(data.performance.bar_count)} />
            <MetricItem label="Signals Generated" value={fmt.num(data.performance.generated_signals)} />
            <MetricItem label="Orders Submitted" value={fmt.num(data.performance.orders_submitted)} />
            <MetricItem label="Fills Received" value={fmt.num(data.performance.fills_received)} />
            <MetricItem label="Rejected Orders" value={String(data.performance.rejected_orders)} tone={data.performance.rejected_orders > 0 ? "neg" : undefined} />
            <MetricItem label="Consecutive Errors" value={String(data.session.consecutive_errors)} tone={data.session.consecutive_errors > 0 ? "neg" : undefined} />
          </div>
        </Panel>

        <Panel title="Risk & Health" className={isCircuitOpen ? "circuit-open" : undefined}>
          <div className="stack gap-md">
            <div>
              <div className="cg-label">Health</div>
              <StatusIndicator status={data.health.status} />
              {data.health.halt_reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{data.health.halt_reason}</div>}
              {data.health.warnings.length > 0 && (
                <ul className="tag-list mt-sm">
                  {data.health.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}
            </div>
            <div>
              <div className="cg-label">Risk Decision</div>
              <StatusIndicator status={data.risk.decision === "allow" ? "active" : data.risk.decision === "warning" ? "paused" : "stopped"} />
              {data.risk.reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{data.risk.reason}</div>}
            </div>
            <div>
              <div className="cg-label">Circuit Breaker</div>
              <div className="row gap-sm">
                <StatusIndicator status={isCircuitOpen ? "open" : "closed"} />
                <span className="pill">Trips: {data.circuit_breaker.trip_count}</span>
              </div>
              {data.circuit_breaker.reason && <div className="faint mt-sm" style={{ fontSize: 11.5 }}>{data.circuit_breaker.reason}</div>}
            </div>
          </div>
        </Panel>
      </div>

      {/* Positions */}
      <div>
        <div className="pt-section">
          <h2>Positions</h2>
          <Link to={`/paper/positions/${data.deployment.deployment_id}`} className="section-sub">Details ›</Link>
        </div>
        {data.positions.is_flat || !data.positions.open_position ? (
          <Panel subtle>
            <EmptyState title="No open positions" hint="The paper account currently has no open positions." />
          </Panel>
        ) : (
          <Panel subtle>
            <table className="data dense">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Entry</th>
                  <th className="num">Current</th>
                  <th className="num">Unreal. P&L</th>
                  <th className="num">Value</th>
                </tr>
              </thead>
              <tbody>
                <tr className={data.positions.open_position.unrealized_pnl >= 0 ? "" : "row-warning"}>
                  <td>{data.positions.open_position.symbol}</td>
                  <td><StatusIndicator status={data.positions.open_position.side === "long" ? "active" : "stopped"} /></td>
                  <td className="num">{data.positions.open_position.quantity.toLocaleString("en-IN")}</td>
                  <td className="num">{fmt.currency(data.positions.open_position.entry_price)}</td>
                  <td className="num">{fmt.currency(data.positions.open_position.current_price)}</td>
                  <td className={`num ${data.positions.open_position.unrealized_pnl >= 0 ? "pos" : "neg"}`}>{fmt.currency(data.positions.open_position.unrealized_pnl)}</td>
                  <td className="num">{fmt.currency(data.positions.open_position.position_value)}</td>
                </tr>
              </tbody>
            </table>
          </Panel>
        )}
      </div>

      {/* Recent Events */}
      <div>
        <div className="pt-section">
          <h2>Recent Activity</h2>
          <Link to={`/paper/events/${data.deployment.deployment_id}`} className="section-sub">View all ›</Link>
        </div>
        {data.recent_events.recent.length === 0 ? (
          <Panel subtle>
            <EmptyState title="No recent events" hint="Events will appear here as the deployment processes data." />
          </Panel>
        ) : (
          <Panel subtle>
            <div className="event-log">
              {[...data.recent_events.recent].reverse().slice(0, 8).map((ev) => (
                <div key={ev.sequence} className={`el-row${isCriticalEvent(ev.event_type) ? " el-critical" : isWarningEvent(ev.event_type) ? " el-warn" : " el-info"}`}>
                  <span className="el-seq">#{ev.sequence}</span>
                  <span className="el-time">{ev.timestamp ? ev.timestamp.slice(11, 19) : "—"}</span>
                  <span className="el-type">{ev.event_type.replace(/_/g, " ")}</span>
                  <span className="el-msg">{ev.message}</span>
                </div>
              ))}
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function KpiCard({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "pos" | "neg" | "warn" | "accent" | "muted" }) {
  const toneClass = tone === "pos" ? "kpi-pos" : tone === "neg" ? "kpi-neg" : tone === "warn" ? "kpi-warn" : tone === "accent" ? "kpi-accent" : "";
  return (
    <div className={`kpi-card ${toneClass}`}>
      <span className="kpi-label">{label}</span>
      <span className={`kpi-value${tone === "pos" ? " pos" : tone === "neg" ? " neg" : ""}`}>{value}</span>
      {sub && <span className={`kpi-sub${tone === "pos" ? " pos" : tone === "neg" ? " neg" : ""}`}>{sub}</span>}
    </div>
  );
}

function toneForPnl(v: number | null | undefined): "pos" | "neg" | "muted" {
  if (v === null || v === undefined) return "muted";
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "muted";
}

function isCriticalEvent(type: string): boolean {
  return ["order_rejected", "health_warning", "circuit_breaker_tripped", "deployment_stopped"].includes(type);
}

function isWarningEvent(type: string): boolean {
  return ["risk_warning", "order_rejected"].includes(type);
}
