import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import { useApp } from "@/store/AppContext";
import type { PipelineStage } from "@/types";
import { Badge, Panel, HealthDot } from "@/components/ui";
import { fmtAgo } from "@/lib/format";
import { fetchConnectionStatus } from "@/lib/upstox";
import type { ConnectionStatus } from "@/lib/upstox";

type ConnectionState =
  | { kind: "loading" }
  | { kind: "mock" }
  | { kind: "connected"; obtainedAt?: string }
  | { kind: "not_connected" }
  | { kind: "network_error" };

function deriveConnectionState(
  status: ConnectionStatus | null,
  mode: "mock" | "live",
): ConnectionState {
  if (mode === "mock") return { kind: "mock" };
  if (status === null) return { kind: "loading" };
  if (status.connected) {
    return { kind: "connected", obtainedAt: status.obtained_at };
  }
  return { kind: "not_connected" };
}

const CONNECTION_META: Record<
  ConnectionState["kind"],
  { label: string; tone: "pos" | "neg" | "warn" | "muted" }
> = {
  loading: { label: "Checking…", tone: "muted" },
  mock: { label: "Mock", tone: "warn" },
  connected: { label: "Connected", tone: "pos" },
  not_connected: { label: "Not Connected", tone: "neg" },
  network_error: { label: "Network Error", tone: "neg" },
};

function derivePipelineStatus(
  status: string | undefined,
): "ready" | "healthy" | "connected" | "disconnected" | "stale" | "auth_error" | "invalid_data" {
  const s = (status || "").toLowerCase();
  if (s === "connected" || s === "healthy") return "healthy";
  if (s === "disconnected" || s === "stopped" || s === "disabled") return "disconnected";
  if (s === "auth_error") return "auth_error";
  if (s === "invalid_data") return "invalid_data";
  if (s === "stale") return "stale";
  return "ready";
}

export function SystemPage() {
  const { env } = useApp();
  const [stages, setStages] = useState<PipelineStage[]>([]);
  const [stagesError, setStagesError] = useState<string | null>(null);
  const [connState, setConnState] = useState<ConnectionState>({ kind: "loading" });
  const [autonomousStatus, setAutonomousStatus] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setStagesError(null);
    dataSource
      .getPipeline()
      .then((r) => {
        if (alive) setStages(r);
      })
      .catch((err: unknown) => {
        if (alive) setStagesError(err instanceof Error ? err.message : "Failed to load pipeline");
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (env.mode !== "live") {
      setConnState({ kind: "mock" });
      return;
    }
    let alive = true;
    fetchConnectionStatus()
      .then((s) => alive && setConnState(deriveConnectionState(s, "live")))
      .catch(() => alive && setConnState({ kind: "network_error" }));
    return () => { alive = false; };
  }, [env.mode]);

  useEffect(() => {
    let alive = true;
    fetch("/health", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: Record<string, unknown> | null) => {
        if (!alive) return;
        const app = data?.autonomous_paper_pipeline as Record<string, unknown> | undefined;
        const status = app?.status as string | undefined;
        setAutonomousStatus(status || null);
      })
      .catch(() => {
        if (alive) setAutonomousStatus(null);
      });
    return () => { alive = false; };
  }, []);

  const connMeta = CONNECTION_META[connState.kind];

  const pipelineStages: PipelineStage[] = [
    ...stages,
    autonomousStatus
      ? {
          id: "autonomous-paper-pipeline",
          label: "Autonomous Paper Pipeline",
          status: derivePipelineStatus(autonomousStatus),
          lastActivity: env.autonomousPaperPipeline?.lastActivity ? new Date(env.autonomousPaperPipeline.lastActivity).getTime() : null,
          metric: autonomousStatus,
        }
      : {
          id: "autonomous-paper-pipeline",
          label: "Autonomous Paper Pipeline",
          status: "disconnected",
          lastActivity: null,
          metric: "not_configured",
        },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">System</h1>
          <div className="subtitle">Pipeline health & runtime environment.</div>
        </div>
        <span className="mock-badge"><span className="dot" /> {env.mode === "mock" ? "Demo Data" : "Live"}</span>
      </div>

      <div className="grid cols-3" style={{ gap: 16 }}>
        <Panel title="Environment"><EnvRow k="Environment" v={env.environment} /></Panel>
        <Panel title="Data Source"><EnvRow k="Data Source" v={env.dataSource} /></Panel>
        <Panel title="Paper Execution"><EnvRow k="Paper Execution" v={env.paperExecution || env.execution} tone={(env.paperExecution || env.execution) === "DISABLED" ? "pos" : "warn"} /></Panel>
        <Panel title="Live Execution"><EnvRow k="Live Execution" v={env.liveExecution || "DISABLED"} tone={(env.liveExecution || "DISABLED") === "DISABLED" ? "pos" : "neg"} /></Panel>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head">
          <span className="panel-title">Upstox Connection</span>
        </div>
        <div className="stat">
          <span className="label">Status</span>
          <span className={`value ${connMeta.tone}`} style={{ fontSize: 15 }}>
            {connMeta.label}
          </span>
        </div>
        {connState.kind === "connected" && connState.obtainedAt && (
          <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
            Connected {new Date(connState.obtainedAt).toLocaleString()}
          </div>
        )}
        {connState.kind === "network_error" && (
          <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
            Unable to reach the server. Check your network connection.
          </div>
        )}
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><span className="panel-title">Pipeline</span></div>
        {stagesError ? (
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">⚠</div>
            <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>Unable to load pipeline</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)" }}>{stagesError}</div>
          </div>
        ) : (
          <div className="pipeline-flow">
            {pipelineStages.map((s, i) => (
              <div className="pf-node" key={s.id}>
                <div className="pf-left">
                  <HealthDot status={s.status} pulse />
                </div>
                <div className="pf-body">
                  <div className="pf-label">{s.label}</div>
                  <div className="pf-metric">{s.metric}</div>
                  <div className="pf-time">last activity {fmtAgo(s.lastActivity)}</div>
                </div>
                <div className="pf-right">
                  <Badge kind={s.status}>{s.status.replace("_", " ")}</Badge>
                </div>
                {i < pipelineStages.length - 1 && <div className="pf-connector" aria-hidden="true" />}
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>
        Pipeline status reflects the {env.mode === "mock" ? "mock" : "live"} data source.
        Connection state is derived from the server-side Upstox token verification.
      </p>
    </>
  );
}

function EnvRow({ k, v, tone }: { k: string; v: string; tone?: "pos" | "neg" | "warn" | "muted" }) {
  return (
    <div className="stat">
      <span className="label">{k}</span>
      <span className={`value ${tone ?? ""}`} style={{ fontSize: 15 }}>{v}</span>
    </div>
  );
}
