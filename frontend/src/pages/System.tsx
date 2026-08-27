import { useEffect, useState } from "react";
import { dataSource } from "@/data/MarketDataSource";
import { useApp } from "@/store/AppContext";
import type { PipelineStage } from "@/types";
import { Badge, Panel } from "@/components/ui";
import { fmtHM } from "@/lib/format";

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
      </div>

      <div className="grid cols-3" style={{ gap: 16 }}>
        <Panel title="Environment"><EnvRow k="Environment" v={env.environment} /></Panel>
        <Panel title="Data Source"><EnvRow k="Data Source" v={env.dataSource} /></Panel>
        <Panel title="Execution"><EnvRow k="Execution" v={env.execution} tone={env.execution === "DISABLED" ? "pos" : "neg"} /></Panel>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><span className="panel-title">Pipeline</span></div>
        <div className="pipe">
          {stages.map((s, i) => (
            <div key={s.id}>
              <div className="pipe-stage">
                <span style={{ fontWeight: 600 }}>{s.label}</span>
                <span className="pipe-metric">{s.metric}</span>
                <Badge kind={s.status}>{s.status.replace("_", " ")}</Badge>
              </div>
              {i < stages.length - 1 && <div className="pipe-arrow">↓</div>}
            </div>
          ))}
        </div>
      </div>

      <p className="faint" style={{ fontSize: 11, marginTop: 16 }}>
        Mock pipeline status. Each stage maps to a real backend component (FYERS → events → bus → candle
        pipeline → data health → snapshot → AI → signal). Connection state is OFFLINE / MOCK — not connected to FYERS.
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

void fmtHM;
