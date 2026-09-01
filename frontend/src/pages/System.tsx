import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import { useApp } from "@/store/AppContext";
import type { PipelineStage } from "@/types";
import { Badge, Panel, HealthDot } from "@/components/ui";
import { fmtAgo } from "@/lib/format";

export function SystemPage() {
  const { env } = useApp();
  const [stages, setStages] = useState<PipelineStage[]>([]);
  useEffect(() => {
    let alive = true;
    dataSource.getPipeline().then((r) => alive && setStages(r));
    return () => { alive = false; };
  }, []);

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
        Mock pipeline status. Each stage maps to a real backend component (Upstox → events → bus → candle
        pipeline → data health → snapshot → AI → signal). Connection state is OFFLINE / MOCK — not connected to Upstox.
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
