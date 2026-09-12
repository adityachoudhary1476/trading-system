"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { paperApi } from "@/lib/paperApi";
import { Link } from "react-router-dom";
import { type WidgetInstance } from "@/store/terminal";

interface ResearchArtifact {
  artifact_id: string;
  strategy_id: string;
  artifact_type: string;
  score: number;
  status: string;
  created_at: string;
  metadata: Record<string, unknown>;
}

interface ResearchArtifactsResponse {
  artifacts: ResearchArtifact[];
}

interface EvolutionGeneration {
  generation_id: string;
  generation: number;
  best_score: number;
  population_size: number;
  mutations: number;
  crossovers: number;
  top_strategies: Array<{ name: string; score: number }>;
}

interface EvolutionResponse {
  generations: EvolutionGeneration[];
}

interface PromotionCandidate {
  strategy_id: string;
  research_score: number;
  walk_forward_score: number;
  paper_score: number;
  composite_score: number;
  status: string;
}

interface PromotionResponse {
  candidates: PromotionCandidate[];
}

export function AiResearchWidget({ widget }: { widget: WidgetInstance }) {
  const [tab, setTab] = useState<"artifacts" | "evolution" | "promotion">("artifacts");

  const { data: artifactsData } = useQuery<ResearchArtifactsResponse>({
    queryKey: ["research-artifacts"],
    queryFn: async () => {
      try {
        const res = await paperApi.request("/research/artifacts?limit=50");
        return res as ResearchArtifactsResponse;
      } catch {
        return { artifacts: [] };
      }
    },
    refetchInterval: 60_000,
  });

  const { data: evolutionData } = useQuery<EvolutionResponse>({
    queryKey: ["strategy-evolution"],
    queryFn: async () => {
      try {
        const res = await paperApi.request("/research/evolution?limit=20");
        return res as EvolutionResponse;
      } catch {
        return { generations: [] };
      }
    },
    refetchInterval: 60_000,
  });

  const { data: promotionData } = useQuery<PromotionResponse>({
    queryKey: ["promotion-candidates"],
    queryFn: async () => {
      try {
        const res = await paperApi.request("/research/promotion?limit=20");
        return res as PromotionResponse;
      } catch {
        return { candidates: [] };
      }
    },
    refetchInterval: 60_000,
  });

  const artifacts = artifactsData?.artifacts ?? [];
  const generations = evolutionData?.generations ?? [];
  const candidates = promotionData?.candidates ?? [];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between p-2 shrink-0 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] text-[var(--text-dim)]">AI RESEARCH</span>
        <div className="flex gap-1">
          {["artifacts", "evolution", "promotion"].map((t) => (
            <button
              key={t}
              onClick={() => setTab(t as typeof tab)}
              className={`term-btn text-[10px] px-2 py-1 ${tab === t ? "active" : ""}`}
            >
              {t.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {tab === "artifacts" && (
          <div className="p-2">
            <div className="dim text-[9px] uppercase tracking-wider mb-2">RESEARCH ARTIFACTS</div>
            {artifacts.length === 0 ? (
              <div className="p-4 text-center dim">No research artifacts found</div>
            ) : (
              <table className="data-table w-full" style={{ fontSize: 11 }}>
                <thead className="sticky top-0 bg-[var(--bg-elev)]">
                  <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                    <th className="p-1 text-left">ID</th>
                    <th className="p-1 text-left">Strategy</th>
                    <th className="p-1 text-right">Type</th>
                    <th className="p-1 text-right">Score</th>
                    <th className="p-1 text-right">Status</th>
                    <th className="p-1 text-right">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {artifacts.map((a: ResearchArtifact, i: number) => (
                    <tr key={a.artifact_id ?? i} className="border-b border-[var(--border-soft)]">
                      <td className="p-1 font-mono text-[10px] truncate max-w-[80px]">{a.artifact_id.slice(0, 12)}…</td>
                      <td className="p-1 text-left truncate max-w-[120px] font-mono text-[10px]">{a.strategy_id}</td>
                      <td className="p-1 text-right font-mono text-[10px]">{a.artifact_type}</td>
                      <td className="p-1 text-right font-mono text-[10px] amber">{a.score.toFixed(2)}</td>
                      <td className="p-1 text-right">
                        <span className={`pill ${a.status === "approved" ? "pos" : a.status === "pending" ? "dim" : "down"} text-[9px]`}>
                          {a.status}
                        </span>
                      </td>
                      <td className="p-1 text-right font-mono text-[10px]">{a.created_at.slice(0, 16)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {tab === "evolution" && (
          <div className="p-2">
            <div className="dim text-[9px] uppercase tracking-wider mb-2">STRATEGY EVOLUTION LAB</div>
            {generations.length === 0 ? (
              <div className="p-4 text-center dim">No evolution data yet. Run the strategy lab to generate generations.</div>
            ) : (
              <div className="space-y-2">
                {generations.map((gen: any, i: number) => (
                  <div key={gen.generation_id ?? i} className="p-2 bg-[var(--bg-elev)] border border-[var(--border-soft)] rounded">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-bold text-[11px]">Generation {gen.generation}</span>
                      <span className="pill text-[9px] pos">Best: {gen.best_score?.toFixed(2) ?? "—"}</span>
                    </div>
                    <div className="dim text-[10px]">Population: {gen.population_size} · Mutations: {gen.mutations} · Crossovers: {gen.crossovers}</div>
                    <div className="flex gap-2 mt-1 flex-wrap">
                      {gen.top_strategies?.slice(0, 3).map((s: any, j: number) => (
                        <span key={j} className="pill text-[9px]">{s.name}: {s.score.toFixed(2)}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === "promotion" && (
          <div className="p-2">
            <div className="dim text-[9px] uppercase tracking-wider mb-2">PROMOTION CANDIDATES</div>
            {candidates.length === 0 ? (
              <div className="p-4 text-center dim">No promotion candidates</div>
            ) : (
              <table className="data-table w-full" style={{ fontSize: 11 }}>
                <thead className="sticky top-0 bg-[var(--bg-elev)]">
                  <tr className="border-b border-[var(--border)] text-[9px] text-[var(--text-faint)]">
                    <th className="p-1 text-left">Strategy</th>
                    <th className="p-1 text-right">Research</th>
                    <th className="p-1 text-right">Walk-Fwd</th>
                    <th className="p-1 text-right">Paper</th>
                    <th className="p-1 text-right">Composite</th>
                    <th className="p-1 text-right">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c: any, i: number) => (
                    <tr key={c.strategy_id ?? i} className="border-b border-[var(--border-soft)]">
                      <td className="p-1 text-left truncate max-w-[140px] font-mono text-[10px]">
                        <Link to={`/paper/strategies/${c.strategy_id}`} className="td-id">{c.strategy_id}</Link>
                      </td>
                      <td className="p-1 text-right font-mono text-[10px]">{c.research_score?.toFixed(2) ?? "—"}</td>
                      <td className="p-1 text-right font-mono text-[10px]">{c.walk_forward_score?.toFixed(2) ?? "—"}</td>
                      <td className="p-1 text-right font-mono text-[10px]">{c.paper_score?.toFixed(2) ?? "—"}</td>
                      <td className="p-1 text-right font-mono text-[10px] font-bold amber">{c.composite_score?.toFixed(2) ?? "—"}</td>
                      <td className="p-1 text-right">
                        <span className={`pill ${c.status === "promoted" ? "pos" : c.status === "eligible" ? "dim" : "down"} text-[9px]`}>
                          {c.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        AI Research Lab · Artifacts → Evolution → Promotion pipeline
      </div>
    </div>
  );
}