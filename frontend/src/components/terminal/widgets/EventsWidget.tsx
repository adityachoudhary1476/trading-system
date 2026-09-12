"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { type WidgetInstance } from "@/store/terminal";
import { EventsResponse } from "@/types/paper-api";

const EVENT_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  order_rejected: { bg: "rgba(255,92,108,0.1)", text: "#ff5c6c", border: "rgba(255,92,108,0.3)" },
  health_warning: { bg: "rgba(242,179,71,0.1)", text: "#f2b347", border: "rgba(242,179,71,0.3)" },
  circuit_breaker_tripped: { bg: "rgba(255,92,108,0.15)", text: "#ff5c6c", border: "rgba(255,92,108,0.4)" },
  deployment_stopped: { bg: "rgba(255,92,108,0.1)", text: "#ff5c6c", border: "rgba(255,92,108,0.3)" },
  circuit_breaker_reset: { bg: "rgba(242,179,71,0.1)", text: "#f2b347", border: "rgba(242,179,71,0.3)" },
  session_restored: { bg: "rgba(46,194,126,0.1)", text: "#2ec27e", border: "rgba(46,194,126,0.3)" },
  deployment_paused: { bg: "rgba(242,179,71,0.1)", text: "#f2b347", border: "rgba(242,179,71,0.3)" },
  deployment_resumed: { bg: "rgba(46,194,126,0.1)", text: "#2ec27e", border: "rgba(46,194,126,0.3)" },
  fill_received: { bg: "rgba(46,194,126,0.1)", text: "#2ec27e", border: "rgba(46,194,126,0.3)" },
  signal_generated: { bg: "rgba(76,141,255,0.1)", text: "#4c8dff", border: "rgba(76,141,255,0.3)" },
};

export function EventsWidget({ _widget }: { _widget: WidgetInstance }) {
  const [deploymentId, setDeploymentId] = useState<string>("");

  const { data: deployments } = useQuery({
    queryKey: ["paper-deployments"],
    queryFn: () => paperApi.listDeployments({ limit: 50 }),
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (deployments?.ok && deployments.data.deployments.length > 0 && !deploymentId) {
      setDeploymentId(deployments.data.deployments[0].deployment_id);
    }
  }, [deployments, deploymentId]);

  const [eventTypeFilter, setEventTypeFilter] = useState<string>("all");
  const [limit, setLimit] = useState(50);

  const { data: eventsResult, isLoading } = useQuery({
    queryKey: ["paper-events", deploymentId, eventTypeFilter, limit],
    queryFn: () => paperApi.getEvents(deploymentId, {
      event_type: eventTypeFilter !== "all" ? eventTypeFilter : undefined,
      limit,
    }),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
  });

  // Type guard for ApiResult.ok === true
  const isEventsResultOk = (result: typeof eventsResult): result is { ok: true; data: EventsResponse } => {
    return result !== undefined && result.ok === true;
  };

  // Extract events data safely
  const eventsData = isEventsResultOk(eventsResult) ? eventsResult.data.events : undefined;

  const allEventTypes = useMemo(() => {
    if (!eventsData?.recent) return ["all"];
    const types = new Set(eventsData.recent.map((e) => e.event_type));
    return ["all", ...Array.from(types).sort()];
  }, [eventsData?.recent]);

  if (!deploymentId) {
    return <div className="p-2 dim text-center">No paper deployments found</div>;
  }

  const events = eventsData?.recent ?? [];
  const reversed = [...events].reverse();
  
  const totalEvents = eventsData?.total_events ?? 0;
  const lastEventSeq = eventsData?.last_event_sequence ?? 0;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">EVENTS</span>
        <span className="flex-1" />
        <select
          value={eventTypeFilter}
          onChange={(e) => setEventTypeFilter(e.target.value)}
          className="bg-[var(--bg-elev-2)] border border-[var(--border)] rounded px-2 py-1 text-[var(--text)] text-[10px]"
        >
          {allEventTypes.map((t) => (
            <option key={t} value={t}>{t === "all" ? "All Events" : t.replace(/_/g, " ").toUpperCase()}</option>
          ))}
        </select>
        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="bg-[var(--bg-elev-2)] border border-[var(--border)] rounded px-2 py-1 text-[var(--text)] text-[10px] w-[80px]"
        >
          <option value={25}>25</option>
          <option value={50}>50</option>
          <option value={100}>100</option>
          <option value={200}>200</option>
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-center dim">Loading events…</div>
        ) : events.length === 0 ? (
          <div className="p-4 text-center dim">No events found</div>
        ) : (
          <div className="divide-y divide-[var(--border-soft)]">
            {reversed.map((event) => {
              const colors = EVENT_COLORS[event.event_type] || { bg: "transparent", text: "var(--text-dim)", border: "transparent" };
              const time = event.timestamp ? event.timestamp.slice(11, 19) : "—";
              const key = event.sequence ?? event.timestamp ?? Math.random().toString(36).slice(2);
              return (
                <div
                  key={key}
                  className="px-2 py-1.5 hover:bg-[#161616]"
                  style={{ backgroundColor: colors.bg }}
                >
                  <div className="flex items-start gap-2">
                    <span className="font-mono text-[10px] text-[var(--text-faint)] shrink-0 w-[50px]">{time}</span>
                    <span
                      className="font-mono text-[10px] shrink-0 w-[28px] text-right"
                      style={{ color: colors.text }}
                    >
                      #{event.sequence}
                    </span>
                    <span
                      className="font-mono text-[9px] uppercase tracking-wider shrink-0 w-[28px]"
                      style={{ color: colors.text }}
                    >
                      {event.event_type.replace(/_/g, " ")}
                    </span>
                    <div className="flex-1 min-w-0 text-[11px]" style={{ color: colors.text }}>
                      {event.message || "—"}
                    </div>
                    {event.message && (
                    <span className="font-mono text-[10px] shrink-0" style={{ color: "var(--text-faint)" }}>
                      {event.message}
                    </span>
                  )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        {totalEvents} total events · Last #{lastEventSeq ?? "—"}
      </div>
    </div>
  );
}