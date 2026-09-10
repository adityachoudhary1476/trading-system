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
  OptionsCapabilityResponse,
  OptionsProviderStatus,
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
  const [scan, setScan] = useState<SectionState<AutonomousScan>>({ status: "idle" });
  const [decisions, setDecisions] = useState<TradingDecision[]>([]);
  const [events, setEvents] = useState<AutonomousEventsResponse["events"]>([]);
  const [deployments, setDeployments] = useState<
    SectionState<AutonomousDeploymentSummary[]>
  >({ status: "idle" });
  const [optionsCapability, setOptionsCapability] = useState<
    SectionState<OptionsCapabilityResponse>
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
    setScan({ status: "loading" });
    try {
      const res: AutonomousScanResponse = await paperApi.getAutonomousScan();
      setScan({ status: "ok", data: res.scan });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load scan";
      setScan({ status: "error", message });
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

  const fetchOptionsCapability = async (
    deploymentList: AutonomousDeploymentSummary[]
  ) => {
    if (!deploymentList || deploymentList.length === 0) {
      setOptionsCapability({ status: "idle" });
      return;
    }
    const target = deploymentList[0];
    setOptionsCapability({ status: "loading" });
    try {
      const res = await paperApi.getOptionsCapability(target.deployment_id);
      setOptionsCapability({ status: "ok", data: res });
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load options capability";
      setOptionsCapability({ status: "error", message });
    }
  };

  const fetchAll = async () => {
    setLoading(true);
    setError(null);
    const results = await Promise.allSettled([
      fetchBot(),
      fetchScan(),
      fetchEvents(),
      fetchDeployments(),
    ]);
    // After deployments are populated, fetch capability for the first one.
    setDeployments((prev) => {
      if (prev.status === "ok" && Array.isArray(prev.data)) {
        void fetchOptionsCapability(prev.data);
      }
      return prev;
    });
    void results;
    setLoading(false);
  };

  const handleLifecycle = async (action: "start" | "pause" | "resume" | "stop") => {
    setActionLoading(true);
    try {
      const res = await paperApi.setAutonomousBotLifecycle(action);
      if (!res.success) {
        setError(res.message);
        return;
      }
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

  // Safe accessor for scan candidates — the live backend returns
  // ``scan.candidates`` (Phase 8C-B); an older shim returned ``scan.signals``.
  // Both shapes are tolerated here so the page never crashes on shape drift.
  function getScanCandidates(s: AutonomousScan | undefined | null) {
    if (!s) return [];
    const c = (s as { candidates?: unknown }).candidates;
    if (Array.isArray(c)) return c as AutonomousScan["candidates"];
    const sig = (s as { signals?: unknown }).signals;
    if (Array.isArray(sig)) return sig as AutonomousScan["candidates"];
    return [];
  }
  function getScanTotalSymbols(s: AutonomousScan | undefined | null): number {
    if (!s) return 0;
    if (typeof s.universe_size === "number") return s.universe_size;
    if (typeof s.total_symbols === "number") return s.total_symbols;
    return 0;
  }
  function getScanTotalSignals(
    s: AutonomousScan | undefined | null,
    candidatesLen: number
  ): number {
    if (!s) return candidatesLen;
    if (typeof s.eligible_count === "number") return s.eligible_count;
    if (typeof s.total_signals === "number") return s.total_signals;
    return candidatesLen;
  }

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

  const scheduler = bot?.scheduler;
  const schedulerStatus = scheduler
    ? scheduler.worker_required
      ? scheduler.worker_alive
        ? ("running" as const)
        : ("not_running" as const)
      : ("not_required" as const)
    : "unknown";

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
          {/* Phase 24A — explicit scheduler liveness indicator.  The bot
              lifecycle and the autonomous scheduler worker are separate
              processes; this makes the distinction visible. */}
          {scheduler && (
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid var(--panel-border)",
                background:
                  schedulerStatus === "running"
                    ? "rgba(34,197,94,0.10)"
                    : schedulerStatus === "not_running"
                      ? "rgba(245,158,11,0.10)"
                      : "rgba(148,163,184,0.08)",
                color:
                  schedulerStatus === "running"
                    ? "#22c55e"
                    : schedulerStatus === "not_running"
                      ? "#f59e0b"
                      : "#94a3b8",
                fontSize: 12,
                fontWeight: 600,
              }}
              title={
                scheduler.worker_required
                  ? scheduler.worker_alive
                    ? `Scheduler running; last tick ${scheduler.last_tick_at || "—"}`
                    : "Scheduler NOT running — bot is RUNNING but no worker ticks"
                  : "No ACTIVE deployments; scheduler not required"
              }
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: "currentColor",
                  opacity: schedulerStatus === "running" ? 1 : 0.5,
                }}
              />
              Scheduler: {schedulerStatus === "running" ? "RUNNING" : schedulerStatus === "not_running" ? "NOT RUNNING" : "N/A"}
            </div>
          )}
          <div style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
            {bot && bot.state !== "running" && (
              <Button
                variant="primary"
                size="sm"
                disabled={actionLoading}
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
            {bot && bot.state === "error" && bot.safety?.kill_switch_state === "active" && (
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

      {/* Phase 24A — Scheduler liveness panel.  Shows whether the separate
          autonomous scheduler worker is actually ticking, so the operator
          can distinguish "bot RUNNING but no worker" from fully
          operational. */}
      {bot && bot.scheduler && (
        <Panel title="Scheduler Worker">
          <div className="metric-grid">
            <MetricItem
              label="Worker required"
              value={bot.scheduler.worker_required ? "yes" : "no"}
            />
            <MetricItem
              label="Worker alive"
              value={
                <Pill tone={bot.scheduler.worker_alive ? "pos" : "neg"}>
                  {bot.scheduler.worker_alive ? "yes" : "no"}
                </Pill>
              }
            />
            <MetricItem
              label="Last tick"
              value={bot.scheduler.last_tick_at || "—"}
            />
            <MetricItem
              label="Decisions"
              value={(bot.decision_count ?? 0).toString()}
            />
          </div>
          {bot.scheduler.worker_required && !bot.scheduler.worker_alive && (
            <p className="muted" style={{ marginTop: 10, fontSize: 12 }}>
              The bot lifecycle is RUNNING but the autonomous scheduler worker
              is not ticking.  Start the worker with{" "}
              <code>AUTONOMOUS_SCHEDULER_ENABLED=true</code> and run{" "}
              <code>python -m backend.autonomous_scheduler</code> as a
              background process.
            </p>
          )}
          {bot.scheduler.deployments.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <p className="muted" style={{ marginBottom: 6, fontSize: 12 }}>
                Per-deployment scheduler liveness
              </p>
              <div style={{ overflowX: "auto" }}>
                <table className="data dense">
                  <thead>
                    <tr>
                      <th>Deployment</th>
                      <th>Liveness</th>
                      <th>Last tick</th>
                      <th>Last data</th>
                      <th>Last decision</th>
                      <th>Last execution</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bot.scheduler.deployments.map((d) => (
                      <tr key={d.deployment_id ?? Math.random()}>
                        <td className="td-id">{d.deployment_id ?? "—"}</td>
                        <td>
                          <Pill
                            tone={
                              d.liveness === "worker_alive"
                                ? "pos"
                                : d.liveness === "market_closed"
                                  ? undefined
                                  : "neg"
                            }
                          >
                            {d.liveness}
                          </Pill>
                        </td>
                        <td className="td-muted">{d.last_tick_at || "—"}</td>
                        <td className="td-muted">{d.last_market_data_at || "—"}</td>
                        <td className="td-muted">{d.last_decision_at || "—"}</td>
                        <td className="td-muted">{d.last_execution_at || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
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
        Array.isArray(deployments.data) &&
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
      {deployments.status === "ok" && Array.isArray(deployments.data) && deployments.data.length === 0 && (
        <Panel title="Active Deployments">
          <EmptyState
            title="No active deployments"
            hint="The autonomous bot has not linked any paper deployments yet."
          />
        </Panel>
      )}

      {/* Options capability surface (Phase A — observational only). */}
      <Panel title="Options Capability">
        {optionsCapability.status === "idle" && (
          <EmptyState
            title="Options capability not yet probed"
            hint={
              deployments.status === "ok" &&
              Array.isArray(deployments.data) &&
              deployments.data.length === 0
                ? "No deployments available to probe for options capability."
                : "Waiting for deployment data to probe options capability."
            }
          />
        )}
        {optionsCapability.status === "loading" && (
          <Loading label="Probing options capability…" />
        )}
        {optionsCapability.status === "error" && (
          <EmptyState
            title="Options capability unavailable"
            hint={`Could not probe options capability: ${optionsCapability.message}. The autonomous bot remains operational; this section will refresh automatically.`}
          />
        )}
        {optionsCapability.status === "ok" && (
          <OptionsCapabilityView data={optionsCapability.data} />
        )}
      </Panel>

      {/* Current Scan Results — never crashes if scan fails or returns partial data */}
      {scan.status === "loading" && (
        <Panel title="Current Market Scan">
          <Loading label="Loading scan…" />
        </Panel>
      )}
      {scan.status === "error" && (
        <Panel title="Current Market Scan">
          <EmptyState
            title="Scan unavailable"
            hint={`Could not run market scan: ${scan.message}. The autonomous bot remains operational; this section will refresh automatically.`}
          />
          <Button variant="secondary" size="sm" onClick={fetchScan}>
            Retry
          </Button>
        </Panel>
      )}
      {scan.status === "ok" && (() => {
        const candidates = getScanCandidates(scan.data);
        const totalSymbols = getScanTotalSymbols(scan.data);
        const totalSignals = getScanTotalSignals(scan.data, candidates.length);
        if (candidates.length === 0) {
          return (
            <Panel title="Current Market Scan">
              <EmptyState
                title="No signals found"
                hint="The market scan completed but found no qualifying signals."
              />
            </Panel>
          );
        }
        return (
          <Panel title="Current Market Scan">
            <p className="muted" style={{ marginBottom: 8 }}>
              Scan ID: <span className="mono">{scan.data.scan_id}</span> •{" "}
              {totalSignals} signals across {totalSymbols} symbols
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
                  {candidates.map((signal: any, i: number) => (
                    <tr key={`${signal.symbol}-${signal.strategy_id}-${i}`}>
                      <td>{signal.symbol}</td>
                      <td>{signal.timeframe}</td>
                      <td>
                        {getActionBadge(signal.signal_action ?? signal.action ?? "HOLD")}
                      </td>
                      <td className="mono">{signal.strategy_id}</td>
                      <td>{getConfidenceBadge(signal.confidence ?? 0)}</td>
                      <td className="td-muted">{signal.signal_timestamp ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        );
      })()}

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
        {(() => {
          const safeDecisions = Array.isArray(decisions) ? decisions : [];
          if (safeDecisions.length === 0) {
            return (
              <EmptyState
                title="No decisions"
                hint="No trading decisions have been generated yet. Start the bot to begin."
              />
            );
          }
          const validCount = safeDecisions.filter(
            (d) => (d as { is_valid?: boolean }).is_valid === true
          ).length;
          return (
            <>
              <p className="muted" style={{ marginBottom: 8 }}>
                {validCount} valid / {safeDecisions.length} total decisions
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
                    {safeDecisions.map((decision) => (
                      <tr key={(decision as { decision_id?: string }).decision_id ?? Math.random()}>
                        <td>{(decision as { symbol?: string }).symbol ?? "—"}</td>
                        <td className="mono">{(decision as { strategy_id?: string }).strategy_id ?? "—"}</td>
                        <td>{(decision as { timeframe?: string }).timeframe ?? "—"}</td>
                        <td>{getActionBadge((decision as { action?: string }).action ?? "HOLD")}</td>
                        <td>{getConfidenceBadge((decision as { confidence?: number }).confidence ?? 0)}</td>
                        <td>
                          <Badge kind={(decision as { is_valid?: boolean }).is_valid ? "healthy" : "disconnected"}>
                            {(decision as { is_valid?: boolean }).is_valid ? "Yes" : "No"}
                          </Badge>
                        </td>
                        <td>{(decision as { status?: string }).status ?? "—"}</td>
                        <td className="td-muted">{(decision as { exclusion_reason?: string }).exclusion_reason || "-"}</td>
                        <td className="td-muted">{(decision as { decision_timestamp?: string }).decision_timestamp ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          );
        })()}
      </Panel>

      {/* Event Log */}
      <Panel
        title="Event Log"
        actions={
          <span className="muted">
            {Array.isArray(events) ? events.length : 0} recent events
          </span>
        }
      >
        {(() => {
          const safeEvents = Array.isArray(events) ? events : [];
          if (safeEvents.length === 0) {
            return <EmptyState title="No events" hint="The event log is empty." />;
          }
          return (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                maxHeight: 384,
                overflowY: "auto",
              }}
            >
              {safeEvents.map((event) => (
                <div
                  key={(event as { event_id?: string }).event_id ?? Math.random()}
                  style={{
                    display: "flex",
                    gap: 10,
                    alignItems: "flex-start",
                    padding: "8px 0",
                    borderBottom: "1px solid var(--panel-border-soft)",
                  }}
                >
                  <Pill tone={eventPillTone((event as { event_type?: string }).event_type ?? "")}>
                    {(event as { event_type?: string }).event_type ?? "unknown"}
                  </Pill>
                  <div style={{ flex: 1 }}>
                    <p style={{ margin: 0 }}>{(event as { message?: string }).message ?? ""}</p>
                    <p className="td-muted" style={{ fontSize: 11, marginTop: 2 }}>
                      {(event as { timestamp?: string }).timestamp ?? "—"} • Symbol: {(event as { symbol?: string | null }).symbol || "—"}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          );
        })()}
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Options capability surface renderer (Phase A — observational only)
//
// IMPORTANT: this view MUST NOT imply that autonomous option execution is
// active. It only describes what the backend has wired up. The autonomous
// scheduler remains equity-only in Phase A.
// ---------------------------------------------------------------------------
function OptionsCapabilityView({
  data,
}: {
  data: OptionsCapabilityResponse;
}) {
  const enabled = data.enabled;
  const capable = data.capable;
  const allowed = data.allowed_option_types ?? [];
  const maxContracts = data.max_contracts_per_trade;
  const providers = data.providers ?? {
    discoverer: { status: "not_configured", detail: "" },
    quote: { status: "not_configured", detail: "" },
    chain: { status: "not_configured", detail: "" },
  };

  // Compose a deterministic phase summary that NEVER claims autonomous
  // execution is active.
  const phase: "disabled" | "enabled_unavailable" | "enabled_capable" =
    !enabled
      ? "disabled"
      : capable
        ? "enabled_capable"
        : "enabled_unavailable";

  const headline: Record<typeof phase, { title: string; body: string }> = {
    disabled: {
      title: "Options trading is disabled for this deployment.",
      body: "The deployment's configuration does not permit option contracts. Autonomous option execution is not enabled.",
    },
    enabled_unavailable: {
      title: "Options are enabled, but required providers are unavailable.",
      body: "The deployment allows option contracts, but the backend's option-data providers are not currently available. Autonomous option execution is not enabled.",
    },
    enabled_capable: {
      title: "Options capability available",
      body: "Option contract discovery and premium data are available. Autonomous option execution is not enabled in this phase.",
    },
  };

  return (
    <div>
      <p style={{ marginTop: 0 }}>
        <strong>{headline[phase].title}</strong>
      </p>
      <p className="muted">{headline[phase].body}</p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: 12,
          marginTop: 12,
        }}
      >
        <MetricItem label="Deployment options_enabled" value={enabled ? "true" : "false"} />
        <MetricItem
          label="Allowed option types"
          value={allowed.length === 0 ? "—" : allowed.join(" / ")}
        />
        <MetricItem
          label="Max contracts / trade"
          value={maxContracts === null || maxContracts === undefined ? "—" : String(maxContracts)}
        />
        <MetricItem label="Backend capable" value={capable ? "true" : "false"} />
      </div>

      <div style={{ marginTop: 16 }}>
        <p className="muted" style={{ marginBottom: 4 }}>
          Provider status
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <ProviderStatusRow name="discoverer" provider={providers.discoverer} />
          <ProviderStatusRow name="quote" provider={providers.quote} />
          <ProviderStatusRow name="chain" provider={providers.chain} />
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <p className="muted" style={{ marginBottom: 4 }}>
          Single-leg capability surface
        </p>
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <li>CE/PE contract discovery (deployment-permitted types only)</li>
          <li>Live option premium quote support</li>
          <li>Paper order metadata support</li>
        </ul>
      </div>

      <div style={{ marginTop: 16 }}>
        <p className="muted" style={{ marginBottom: 4 }}>
          Not yet enabled
        </p>
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <li>Autonomous option execution</li>
          <li>Multi-leg strategies</li>
          <li>Greeks / OI / IV analytics</li>
          <li>Lot-size-correct option accounting</li>
        </ul>
      </div>

      {data.last_error ? (
        <p className="muted" style={{ marginTop: 12 }}>
          Last error: {data.last_error}
        </p>
      ) : null}
    </div>
  );
}

function ProviderStatusRow({
  name,
  provider,
}: {
  name: string;
  provider: OptionsProviderStatus;
}) {
  const tone =
    provider.status === "available"
      ? "pos"
      : provider.status === "disabled"
        ? undefined
        : "warn";
  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        alignItems: "center",
        padding: "4px 0",
      }}
    >
      <Pill tone={tone}>{provider.status}</Pill>
      <span style={{ minWidth: 90 }}>
        <strong>{name}</strong>
      </span>
      <span className="muted" style={{ fontSize: 12 }}>
        {provider.detail || "—"}
      </span>
    </div>
  );
}
