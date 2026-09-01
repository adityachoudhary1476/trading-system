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

export function SystemPage() {
  const { env } = useApp();
  const [stages, setStages] = useState<PipelineStage[]>([]);
  const [connState, setConnState] = useState<ConnectionState>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    dataSource.getPipeline().then((r) => alive && setStages(r));
    return () => { alive = false; };
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

  const connMeta = CONNECTION_META[connState.kind];

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
        <Panel title="Execution"><EnvRow k="Execution" v={env.execution} tone={env.execution === "DISABLED" ? "pos" : "neg"} /></Panel>
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
        <div className="pipeline-flow">
          {stages.map((s, i) => (
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
              {i < stages.length - 1 && <div className="pf-connector" aria-hidden="true" />}
            </div>
          ))}
        </div>
      </div>

      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>
        Pipeline status reflects the {env.mode === "mock" ? "mock" : "live"} data source.
        Connection state is derived from the server-side Upstox token verification.
      </p>
    </>
  );
}

function EnvRow({ k, v, tone }: { k: string; v: string; tone?: "pos" | "neg" }) {
  return (
    <div className="stat">
      <span className="label">{k}</span>
      <span className={`value ${tone ?? ""}`} style={{ fontSize: 15 }}>{v}</span>
    </div>
  );
}
