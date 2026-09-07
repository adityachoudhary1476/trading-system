import { useEffect, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import type {
  AutonomousBot,
  AutonomousBotStatus,
  AutonomousScan,
  AutonomousScanResponse,
  AutonomousDecideResponse,
  AutonomousDeploymentsResponse,
  AutonomousDeploymentSummary,
  AutonomousEventsResponse,
  TradingDecision,
} from "@/types/paper-api";
import {
  Panel,
  Badge,
  Pill,
  Button,
  EmptyState,
  StatusIndicator,
  MetricItem,
  Loading,
} from "@/components/ui";

type SectionState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "error"; message: string };

export default function AutonomousCenter() {
  const [botState, setBotState] = useState<SectionState<AutonomousBot>>({ status: "idle" });
  const [scan, setScan] = useState<AutonomousScan | null>(null);
  const [decisions, setDecisions] = useState<TradingDecision[]>([]);
  const [events, setEvents] = useState<AutonomousEventsResponse["events"]>([]);
  const [deployments, setDeployments] = useState<
    SectionState<AutonomousDeploymentSummary[]>
  >({ status: "idle" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [decisionsLoading, setDecisionsLoading] = useState(false);

  const fetchBot = async () => {
    setBotState({ status: "loading" });
    try {
      const res = await paperApi.getAutonomousBot();
      setBotState({ status: "ok", data: res.bot });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load bot status";
      setBotState({ status: "error", message });
    }
  };

  const fetchScan = async () => {
    try {
      const res: AutonomousScanResponse = await paperApi.getAutonomousScan();
      setScan(res.scan);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load scan");
    }
  };

  const fetchDecisionsNow = async () => {
    setDecisionsLoading(true);
    try {
      const res: AutonomousDecideResponse = await paperApi.getAutonomousDecisions();
      setDecisions(res.decisions || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load decisions");
    } finally {
      setDecisionsLoading(false);
    }
  };

  const fetchEvents = async () => {
    try {
      const res = await paperApi.getAutonomousEvents({ limit: 50 });
      setEvents(res.events || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load events");
    }
  };

  const fetchDeployments = async () => {
    setDeployments({ status: "loading" });
    try {
      const res: AutonomousDeploymentsResponse = await paperApi.getAutonomousDeployments();
      setDeployments({ status: "ok", data: res.deployments || [] });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to load deployments";
      setDeployments({ status: "error", message });
    }
  };

  const fetchAll = async () => {
    setLoading(true);
    setError(null);
    await Promise.allSettled([fetchBot(), fetchScan(), fetchEvents(), fetchDeployments()]);
    setLoading(false);
  };

  const handleLifecycle = async (action: "start" | "pause" | "resume" | "stop") => {
    setActionLoading(true);
    try {
      await paperApi.setAutonomousBotLifecycle(action);
      await fetchBot();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} bot`);
    } finally {
      setActionLoading(false);
    }
  };

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 10000);
    return () => clearInterval(interval);
  }, []);

  const botStatusToIndicator = (status: AutonomousBotStatus): string => {
    switch (status) {
      case "running":
        return "active";
      case "paused":
        return "paused";
      case "stopped":
        return "stopped";
      case "error":
        return "failed";
      default:
        return "stopped";
    }
  };

  const getActionBadge = (action: string) => {
    const kind = action === "BUY" ? "buy" : action === "SELL" ? "sell" : "hold";
    return <Badge kind={kind}>{action}</Badge>;
  };

  const getConfidenceBadge = (confidence: number) => {
    const pct = Math.round(confidence * 100);
    const kind = pct >= 80 ? "healthy" : pct >= 60 ? "stale" : "disconnected";
    return <Badge kind={kind}>{pct}%</Badge>;
  };

  const eventPillTone = (type: string): "pos" | "neg" | "warn" | undefined => {
    if (
      type.includes("ERROR") ||
      type.includes("REJECTED") ||
      type.includes("VIOLATION") ||
      type.includes("HALTED") ||
      type.includes("TRIPPED") ||
      type.includes("STOPPED")
    ) {
      return "neg";
    }
    if (
      type.includes("CREATED") ||
      type.includes("RESTORED") ||
      type.includes("RESUMED") ||
      type.includes("ACTIVATED")
    ) {
      return "pos";
    }
    if (type.includes("PAUSED") || type.includes("WARNING") || type.includes("SAVED")) {
      return "warn";
    }
    return undefined;
  };

  if (loading && botState.status !== "ok") {
    return (
      <div className="empty">
        <Loading label="Loading autonomous dashboard…" />
      </div>
    );
  }

  if (botState.status === "error") {
    return (
      <div className="error-state" role="alert">
        <div className="es-icon" aria-hidden="true">
          !
        </div>
        <div className="es-title">Autonomous engine unavailable</div>
        <div className="es-hint">{botState.message || "Unknown error"}</div>
        <Button variant="secondary" size="sm" onClick={fetchAll}>
          Retry
        </Button>
      </div>
    );
  }

  const bot: AutonomousBot | null =
    botState.status === "ok" ? botState.data : null;

  return (
    <div className="paper-shell">
      {/* Header with status and lifecycle controls */}
      <div className="pt-section">
        <div>
          <h1 className="page-title">Autonomous Trading Operations Center</h1>
          <span className="subtitle">Phase 6 — Paper Trading</span>
        </div>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          {bot && (
            <StatusIndicator status={botStatusToIndicator(bot.state as AutonomousBotStatus)} />
          )}
          <div style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
            {bot && bot.state !== "running" && (
              <Button
                variant="primary"
                size="sm"
                disabled={actionLoading || bot.state === "stopped"}
                onClick={() => handleLifecycle("start")}
              >
                Start
              </Button>
            )}
            {bot && bot.state === "running" && (
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={actionLoading}
                  onClick={() => handleLifecycle("pause")}
                >
                  Pause
                </Button>
                <Button
                  variant="danger-solid"
                  size="sm"
                  disabled={actionLoading}
                  onClick={() => handleLifecycle("stop")}
                >
                  Stop
                </Button>
              </>
            )}
            {bot && bot.state === "paused" && (
              <Button
                variant="primary"
                size="sm"
                disabled={actionLoading}
                onClick={() => handleLifecycle("resume")}
              >
                Resume
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Error banner (non-blocking) */}
      {error && (
        <div
          style={{
            padding: "10px 12px",
            background: "rgba(255,92,108,0.08)",
            border: "1px solid rgba(255,92,108,0.3)",
            borderRadius: 6,
          }}
        >
          <p className="muted">{error}</p>
        </div>
      )}

      {/* Bot Summary Metrics */}
      {bot && (
        <Panel title="Bot Summary">
          <div className="metric-grid">
            <MetricItem label="State" value={bot.state ?? "unknown"} />
            <MetricItem
              label="Uptime"
              value={`${Math.floor((bot.uptime_seconds ?? 0) / 60)}m ${Math.floor((bot.uptime_seconds ?? 0) % 60)}s`}
            />
            <MetricItem
              label="Deployments"
              value={(bot.deployment_count ?? 0).toString()}
            />
            <MetricItem label="Event Count" value={(bot.event_count ?? 0).toString()} />
          </div>
        </Panel>
      )}

      {/* Bot Configuration */}
      {bot && (
        <Panel title="Bot Configuration">
          <div className="metric-grid">
            <MetricItem
              label="Bot ID"
              value={<span className="mono">{bot.bot_id}</span>}
            />
            <MetricItem label="Name" value={bot.name} />
            <MetricItem
              label="Trading Mode"
              value={<Pill>{bot.trading_mode}</Pill>}
            />
            <MetricItem label="Source" value={bot.source} />
            <MetricItem
              label="Allowed Symbols"
              value={(bot.allowed_symbols || []).join(", ") || "—"}
            />
            <MetricItem
              label="Allowed Timeframes"
              value={(bot.allowed_timeframes || []).join(", ") || "—"}
            />
            <MetricItem
              label="Max Positions"
              value={(bot.max_simultaneous_positions ?? 0).toString()}
            />
            <MetricItem
              label="Kill Switch"
              value={
                <Pill
                  tone={
                    bot.safety?.kill_switch_state === "active" ? "pos" : "warn"
                  }
                >
                  {bot.safety?.kill_switch_state ?? "unknown"}
                </Pill>
              }
            />
          </div>
        </Panel>
      )}

      {/* Active Deployments — fetched independently; never crashes the page */}
      {deployments.status === "loading" && (
        <Panel title="Active Deployments">
          <Loading label="Loading deployments…" />
        </Panel>
      )}
      {deployments.status === "error" && (
        <Panel title="Active Deployments">
          <EmptyState
            title="Deployments unavailable"
            hint={`Could not load deployments: ${deployments.message}. The autonomous bot remains operational; this section will refresh automatically.`}
          />
          <Button variant="secondary" size="sm" onClick={fetchDeployments}>
            Retry
          </Button>
        </Panel>
      )}
      {deployments.status === "ok" &&
        deployments.data.length > 0 && (
          <Panel title="Active Deployments">
            <table className="data dense">
              <thead>
                <tr>
                  <th>Deployment ID</th>
                  <th>Symbol</th>
                  <th>Status</th>
                  <th>Strategy</th>
                  <th>Timeframe</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {deployments.data.map((dep) => (
                  <tr key={dep.deployment_id}>
                    <td className="td-id">{dep.deployment_id}</td>
                    <td>{dep.symbol}</td>
                    <td>
                      <StatusIndicator status={dep.status} />
                    </td>
                    <td className="mono">{dep.strategy_id}</td>
                    <td>{dep.timeframe}</td>
                    <td className="td-muted">{dep.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
      {deployments.status === "ok" && deployments.data.length === 0 && (
        <Panel title="Active Deployments">
          <EmptyState
            title="No active deployments"
            hint="The autonomous bot has not linked any paper deployments yet."
          />
        </Panel>
      )}

      {/* Options-specific unavailability banner */}
      <Panel title="Options Data">
        <p className="muted">
          Options-specific data (CE/PE legs, strike, expiry, Greeks, multi-leg P&L) is{" "}
          <strong>Unavailable</strong> — the autonomous pipeline is equity-only.
        </p>
      </Panel>

      {/* Current Scan Results */}
      {scan && scan.signals.length > 0 && (
        <Panel title="Current Market Scan">
          <p className="muted" style={{ marginBottom: 8 }}>
            Scan ID: <span className="mono">{scan.scan_id}</span> •{" "}
            {scan.total_signals} signals across {scan.total_symbols} symbols
          </p>
          <div style={{ overflowX: "auto" }}>
            <table className="data dense">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Timeframe</th>
                  <th>Action</th>
                  <th>Strategy</th>
                  <th>Confidence</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {scan.signals.map((signal, i) => (
                  <tr key={`${signal.symbol}-${signal.strategy_id}-${i}`}>
                    <td>{signal.symbol}</td>
                    <td>{signal.timeframe}</td>
                    <td>{getActionBadge(signal.action)}</td>
                    <td className="mono">{signal.strategy_id}</td>
                    <td>{getConfidenceBadge(signal.confidence)}</td>
                    <td className="td-muted">{signal.signal_timestamp}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {scan && scan.signals.length === 0 && (
        <Panel title="Current Market Scan">
          <EmptyState title="No signals found" hint="The market scan completed but found no qualifying signals." />
        </Panel>
      )}

      {/* Generated Decisions — only fetched on user action */}
      <Panel
        title="Trading Decisions"
        actions={
          <Button
            variant="secondary"
            size="sm"
            disabled={decisionsLoading}
            onClick={fetchDecisionsNow}
          >
            {decisionsLoading ? "Running…" : "Run Decision"}
          </Button>
        }
      >
        {decisions.length > 0 ? (
          <>
            <p className="muted" style={{ marginBottom: 8 }}>
              {decisions.filter((d) => d.is_valid).length} valid / {decisions.length} total decisions
            </p>
            <div style={{ overflowX: "auto" }}>
              <table className="data dense">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Strategy</th>
                    <th>Timeframe</th>
                    <th>Action</th>
                    <th>Confidence</th>
                    <th>Valid</th>
                    <th>Status</th>
                    <th>Exclusion</th>
                    <th>Timestamp</th>
                  </tr>
                </thead>
                <tbody>
                  {decisions.map((decision) => (
                    <tr key={decision.decision_id}>
                      <td>{decision.symbol}</td>
                      <td className="mono">{decision.strategy_id}</td>
                      <td>{decision.timeframe}</td>
                      <td>{getActionBadge(decision.action)}</td>
                      <td>{getConfidenceBadge(decision.confidence)}</td>
                      <td>
                        <Badge kind={decision.is_valid ? "healthy" : "disconnected"}>
                          {decision.is_valid ? "Yes" : "No"}
                        </Badge>
                      </td>
                      <td>{decision.status}</td>
                      <td className="td-muted">{decision.exclusion_reason || "-"}</td>
                      <td className="td-muted">{decision.decision_timestamp}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <EmptyState
            title="No decisions"
            hint="No trading decisions have been generated yet. Start the bot to begin."
          />
        )}
      </Panel>

      {/* Event Log */}
      <Panel
        title="Event Log"
        actions={<span className="muted">{events.length} recent events</span>}
      >
        {events.length > 0 ? (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              maxHeight: 384,
              overflowY: "auto",
            }}
          >
            {events.map((event) => (
              <div
                key={event.event_id}
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "flex-start",
                  padding: "8px 0",
                  borderBottom: "1px solid var(--panel-border-soft)",
                }}
              >
                <Pill tone={eventPillTone(event.event_type)}>{event.event_type}</Pill>
                <div style={{ flex: 1 }}>
                  <p style={{ margin: 0 }}>{event.message}</p>
                  <p className="td-muted" style={{ fontSize: 11, marginTop: 2 }}>
                    {event.timestamp} • Symbol: {event.symbol || "—"}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="No events" hint="The event log is empty." />
        )}
      </Panel>
    </div>
  );
}
