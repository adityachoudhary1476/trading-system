import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { paperApi } from "@/lib/paperApi";
import type { EventsResponse } from "@/types/paper-api";
import { Panel, EmptyState, Loading, Button, StatusIndicator } from "@/components/ui";
import { DeploymentPicker } from "@/components/paper/paperShared";

const EVENT_TONE: Record<string, "el-critical" | "el-warn" | "el-info"> = {
  bar_processed: "el-info",
  order_submitted: "el-info",
  fill_received: "el-info",
  signal_generated: "el-info",
  order_rejected: "el-critical",
  health_warning: "el-critical",
  circuit_breaker_tripped: "el-critical",
  circuit_breaker_reset: "el-warn",
  checkpoint_saved: "el-info",
  session_restored: "el-warn",
  deployment_activated: "el-info",
  deployment_paused: "el-warn",
  deployment_resumed: "el-info",
  deployment_stopped: "el-critical",
};

export function PaperEvents() {
  const { deploymentId } = useParams<{ deploymentId: string }>();
  const [deploymentIdState, setDeploymentIdState] = useState(deploymentId ?? "");
  const [events, setEvents] = useState<EventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [eventType, setEventType] = useState("");
  const [sinceSequence, setSinceSequence] = useState("");
  const [limit, setLimit] = useState("100");
  const [selectedEvent, setSelectedEvent] = useState<number | null>(null);

  useEffect(() => {
    if (deploymentId) setDeploymentIdState(deploymentId);
  }, [deploymentId]);

  const fetch = () => {
    if (!deploymentIdState) return;
    setLoading(true);
    setError(null);
    paperApi.getEvents(deploymentIdState, {
      event_type: eventType || undefined,
      since_sequence: sinceSequence ? parseInt(sinceSequence, 10) : undefined,
      limit: limit ? parseInt(limit, 10) : 100,
    }).then((res) => {
      if (res.ok) {
        setEvents(res.data);
      } else {
        setError(res.error.message);
      }
      setLoading(false);
    });
  };

  useEffect(() => {
    fetch();
  }, [deploymentIdState, eventType, sinceSequence, limit]);

  if (!deploymentIdState) {
    return (
      <div className="paper-shell">
        <div className="pt-section"><h2>Select Deployment</h2></div>
        <DeploymentPicker
          value=""
          onChange={setDeploymentIdState}
          placeholder="Select a deployment to view its events…"
        />
        <EmptyState
          title="No deployment selected"
          hint="Select a deployment above to view its operational event log."
        />
      </div>
    );
  }

  const reversedEvents = events ? [...events.events.recent].reverse() : [];
  const selected = selectedEvent !== null ? reversedEvents.find((e) => e.sequence === selectedEvent) : null;

  return (
    <div className="paper-shell">
      <Panel
        title={`Event Log${events ? ` (${events.events.total_events} total)` : ""}`}
        toolbar={
          <div className="row gap-sm wrap">
            <input
              className="search"
              placeholder="Event type"
              value={eventType}
              onChange={(e) => setEventType(e.target.value)}
              style={{ width: 140 }}
            />
            <input
              className="search"
              placeholder="Since seq #"
              type="number"
              value={sinceSequence}
              onChange={(e) => setSinceSequence(e.target.value)}
              style={{ width: 100 }}
            />
            <input
              className="search"
              placeholder="Limit"
              type="number"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              style={{ width: 70 }}
            />
            <Button variant="primary" size="sm" onClick={fetch}>Apply</Button>
          </div>
        }
      >
        {loading && <Loading label="Loading events…" />}

        {error && (
          <div className="error-state" role="alert">
            <div className="es-icon" aria-hidden="true">!</div>
            <div className="es-title">Unable to load events</div>
            <div className="es-hint">{error}</div>
            <Button variant="secondary" size="sm" onClick={fetch}>Retry</Button>
          </div>
        )}

        {!loading && !error && events && events.events.recent.length === 0 && (
          <EmptyState title="No events" hint="No events match the current filters." />
        )}

        {!loading && !error && events && events.events.recent.length > 0 && (
          <div className="event-log">
            {reversedEvents.map((ev) => (
              <div
                key={ev.sequence}
                className={`el-row ${EVENT_TONE[ev.event_type] ?? "el-info"}${selectedEvent === ev.sequence ? " active" : ""}`}
                onClick={() => setSelectedEvent(selectedEvent === ev.sequence ? null : ev.sequence)}
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === "Enter") setSelectedEvent(ev.sequence); }}
                role="button"
                aria-expanded={selectedEvent === ev.sequence}
              >
                <span className="el-seq">#{ev.sequence}</span>
                <span className="el-time">{ev.timestamp ? ev.timestamp.slice(11, 19) : "—"}</span>
                <span className="el-type">{ev.event_type.replace(/_/g, " ")}</span>
                <span className="el-msg">{ev.message}</span>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* Event Detail */}
      {selected && (
        <Panel title={`Event #${selected.sequence} Detail`}>
          <div className="metric-grid">
            <div className="mg-item">
              <span className="mg-label">Sequence</span>
              <span className="mg-value">{selected.sequence}</span>
            </div>
            <div className="mg-item">
              <span className="mg-label">Timestamp</span>
              <span className="mg-value">{selected.timestamp}</span>
            </div>
            <div className="mg-item">
              <span className="mg-label">Type</span>
              <span className="mg-value"><StatusIndicator status={selected.event_type.includes("rejected") || selected.event_type.includes("halted") || selected.event_type.includes("tripped") ? "stopped" : selected.event_type.includes("warning") ? "warning" : "healthy"} /></span>
            </div>
            <div className="mg-item">
              <span className="mg-label">Deployment</span>
              <span className="mg-value">{selected.deployment_id}</span>
            </div>
            <div className="mg-item" style={{ gridColumn: "span 2" }}>
              <span className="mg-label">Message</span>
              <span className="mg-value">{selected.message}</span>
            </div>
          </div>
        </Panel>
      )}

      {/* Navigation */}
      <Panel title="Navigate">
        <div className="controls-group">
          <Link to={`/paper/deployments/${deploymentIdState}`} className="btn btn-secondary btn-sm">Deployment Detail</Link>
          <Link to={`/paper/sessions/${deploymentIdState}`} className="btn btn-secondary btn-sm">Session</Link>
          <Link to={`/paper/positions/${deploymentIdState}`} className="btn btn-secondary btn-sm">Positions</Link>
          <Link to={`/paper/risk/${deploymentIdState}`} className="btn btn-secondary btn-sm">Risk & Health</Link>
        </div>
      </Panel>
    </div>
  );
}
