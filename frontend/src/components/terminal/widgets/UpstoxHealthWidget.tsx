"use client";

import { useQuery } from "@tanstack/react-query";
import { paperApi } from "@/lib/paperApi";

export function UpstoxHealthWidget({ widget }: { widget: WidgetInstance }) {
  const { data: upstoxData, isLoading, error } = useQuery({
    queryKey: ["upstox-status"],
    queryFn: () => fetch("/api/upstox/status", { cache: "no-store" }).then((r) => r.json()),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const { data: feedHealth } = useQuery({
    queryKey: ["feed-health"],
    queryFn: () => fetch("/api/market/pipeline", { cache: "no-store" }).then((r) => r.json()).catch(() => null),
    refetchInterval: 30_000,
  });

  const connected = upstoxData?.connected === true;
  const provider = upstoxData?.provider ?? "Upstox";
  const obtainedAt = upstoxData?.obtained_at ? new Date(upstoxData.obtained_at) : null;
  const market = upstoxData?.market ?? "NSE";
  const phase = upstoxData?.phase ?? "unknown";
  const serverTime = upstoxData?.serverTime ? new Date(upstoxData.serverTime) : null;
  const nextOpen = upstoxData?.nextOpen ? new Date(upstoxData.nextOpen) : null;
  const nextClose = upstoxData?.nextClose ? new Date(upstoxData.nextClose) : null;

  const latency = upstoxData?.latency_ms ?? null;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">UPSTOX HEALTH</span>
        <span className={`pill ${connected ? "pos" : "neg"} text-[10px]`}>
          {connected ? "CONNECTED" : "DISCONNECTED"}
        </span>
      </div>

      <div className="p-2 shrink-0 border-b border-[var(--border-soft)] grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <div className="dim">Provider</div>
          <div className="font-mono">{provider}</div>
        </div>
        <div>
          <div className="dim">Market</div>
          <div className="font-mono">{market}</div>
        </div>
        <div>
          <div className="dim">Phase</div>
          <div className={`font-mono ${phase === "regular" ? "up" : "dim"}`}>{phase.toUpperCase()}</div>
        </div>
        <div>
          <div className="dim">Latency</div>
          <div className="font-mono">{latency !== null ? `${latency}ms` : "—"}</div>
        </div>
        <div className="col-span-2">
          <div className="dim">Last Data</div>
          <div className="font-mono text-[10px]">{obtainedAt ? obtainedAt.toLocaleTimeString() : "—"}</div>
        </div>
        <div className="col-span-2">
          <div className="dim">Server Time</div>
          <div className="font-mono text-[10px]">{serverTime ? serverTime.toLocaleTimeString() : "—"}</div>
        </div>
        {nextOpen && (
          <div className="col-span-2">
            <div className="dim">Next Open</div>
            <div className="font-mono text-[10px] up">{nextOpen.toLocaleString()}</div>
          </div>
        )}
        {nextClose && (
          <div className="col-span-2">
            <div className="dim">Next Close</div>
            <div className="font-mono text-[10px] down">{nextClose.toLocaleString()}</div>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto p-2">
        <div className="dim text-[9px] uppercase tracking-wider mb-2">PIPELINE STAGES</div>
        {feedHealth && Array.isArray(feedHealth) && feedHealth.length > 0 ? (
          <div className="space-y-1">
            {feedHealth.map((stage: any, i: number) => (
              <div key={i} className="p-2 text-[11px] bg-[var(--bg-elev)] border border-[var(--border-soft)] rounded">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px]">{stage.name || stage.stage || `Stage ${i + 1}`}</span>
                  <span className={`pill ${stage.healthy ? "pos" : "neg"} text-[9px]`}>
                    {stage.healthy ? "HEALTHY" : "DEGRADED"}
                  </span>
                </div>
                {stage.details && (
                  <div className="text-[10px] dim mt-1 font-mono">{stage.details}</div>
                )}
                {stage.last_event_at && (
                  <div className="text-[10px] dim mt-1">Last: {new Date(stage.last_event_at).toLocaleTimeString()}</div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="p-4 text-center dim text-[11px]">Pipeline data unavailable</div>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        {connected ? "●" : "○"} Upstox WebSocket · Market data via Vercel proxy
      </div>
    </div>
  );
}