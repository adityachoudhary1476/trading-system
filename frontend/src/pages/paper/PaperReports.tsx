import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { ExportResponse } from "@/types/paper-api";
import { Panel, EmptyState, Loading, Button } from "@/components/ui";
import { DeploymentPicker } from "@/components/paper/paperShared";

function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="report-section">
      <button className="rs-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>{title}</span>
        <span className="faint">{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="rs-body">{children}</div>}
    </div>
  );
}

function JsonBlock({ data, label }: { data: unknown; label: string }) {
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify(data, null, 2);
  const copy = () => {
    navigator.clipboard.writeText(json).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <div>
      <div className="row between mb-md">
        <span className="faint" style={{ fontSize: 11 }}>{label}</span>
        <Button variant="ghost" size="xs" onClick={copy}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      <pre aria-label={label}>{json}</pre>
    </div>
  );
}

export function PaperReports() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [report, setReport] = useState<ExportResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  useEffect(() => {
    if (!deploymentIdState) return;
    let alive = true;
    setLoading(true);
    setError(null);
    paperApi.exportJson(deploymentIdState).then((res) => {
      if (!alive) return;
      if (res.ok) {
        setReport(res.data);
      } else {
        setError(res.error.message);
      }
      setLoading(false);
    });
    return () => { alive = false; };
  }, [deploymentIdState]);

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Select Deployment</h2></div>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view its reports…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its reports and export data."
        />
      </div>
    );
  }

  const download = () => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `paper-report-${deploymentIdState}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="paper-shell">
      <div className="pt-section">
        <h2>Reports & Export</h2>
        <Button variant="primary" size="sm" onClick={download} disabled={!report}>Export JSON</Button>
      </div>

      {loading && <Loading label="Loading reports…" />}

      {error && (
        <div className="error-state" role="alert">
          <div className="es-icon" aria-hidden="true">!</div>
          <div className="es-title">Unable to load reports</div>
          <div className="es-hint">{error}</div>
        </div>
      )}

      {report && (
        <Panel>
          <CollapsibleSection title="Dashboard Snapshot" defaultOpen>
            <JsonBlock data={report.dashboard_snapshot} label="Full dashboard snapshot" />
          </CollapsibleSection>

          <CollapsibleSection title="Deployment">
            <JsonBlock data={report.dashboard_snapshot.deployment} label="Deployment details" />
          </CollapsibleSection>

          <CollapsibleSection title="Account">
            <JsonBlock data={report.dashboard_snapshot.account} label="Account state" />
          </CollapsibleSection>

          <CollapsibleSection title="Performance">
            <JsonBlock data={report.dashboard_snapshot.performance} label="Performance metrics" />
          </CollapsibleSection>

          <CollapsibleSection title="Health & Risk">
            <JsonBlock data={{ health: report.dashboard_snapshot.health, risk: report.dashboard_snapshot.risk, circuit_breaker: report.dashboard_snapshot.circuit_breaker }} label="Health, risk, circuit breaker" />
          </CollapsibleSection>

          <CollapsibleSection title="Evidence">
            <JsonBlock data={report.dashboard_snapshot.evidence_summary} label="Evidence summary" />
          </CollapsibleSection>

          {report.report && (
            <CollapsibleSection title="Operations Report">
              <JsonBlock data={report.report} label="Operations report" />
            </CollapsibleSection>
          )}
        </Panel>
      )}

      {/* Navigation */}
      <Panel title="Navigate">
        <div className="controls-group">
          <Link to={`/paper/deployments/${deploymentIdState}`} className="btn btn-secondary btn-sm">Deployment Detail</Link>
          <Link to={`/paper/sessions/${deploymentIdState}`} className="btn btn-secondary btn-sm">Session</Link>
          <Link to={`/paper/events/${deploymentIdState}`} className="btn btn-secondary btn-sm">Events</Link>
          <Link to={`/paper/risk/${deploymentIdState}`} className="btn btn-secondary btn-sm">Risk & Health</Link>
        </div>
      </Panel>
    </div>
  );
}
