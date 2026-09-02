import { useMemo, useState } from "react";
import { Panel, EmptyState } from "@/components/ui";
import {
  CATEGORY_LABELS,
  EVIDENCE_LABELS,
  EVIDENCE_TONE,
  STRATEGY_LIBRARY,
  type Category,
  type EvidenceQuality,
  type StrategyLibraryEntry,
} from "@/data/strategyLibrary";

const ALL = "__all__" as const;
const CATEGORIES: (Category | typeof ALL)[] = [
  ALL,
  "trend_following",
  "momentum",
  "mean_reversion",
  "breakout",
  "volatility",
  "market_regime",
  "multi_factor",
];
const EVIDENCE_LEVELS: (EvidenceQuality | typeof ALL)[] = [
  ALL,
  "peer_reviewed",
  "reputable_practitioner",
  "blog_or_marketing",
];

export function PaperResearch() {
  const [category, setCategory] = useState<Category | typeof ALL>(ALL);
  const [evidence, setEvidence] = useState<EvidenceQuality | typeof ALL>(ALL);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(
    STRATEGY_LIBRARY[0]?.candidate_id ?? null,
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return STRATEGY_LIBRARY.filter((c) => {
      if (category !== ALL && c.category !== category) return false;
      if (evidence !== ALL && c.evidence_quality !== evidence) return false;
      if (!q) return true;
      return (
        c.name.toLowerCase().includes(q) ||
        c.candidate_id.toLowerCase().includes(q) ||
        c.claim.toLowerCase().includes(q) ||
        c.source.toLowerCase().includes(q) ||
        c.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [category, evidence, search]);

  const selected: StrategyLibraryEntry | null = useMemo(
    () => filtered.find((c) => c.candidate_id === selectedId) ?? filtered[0] ?? null,
    [filtered, selectedId],
  );

  return (
    <div className="paper-strategies">
      <header className="page-header">
        <div>
          <h2>Strategy Research Library</h2>
          <p className="page-sub">
            Curated, sourced trading-strategy ideas. Each entry carries
            provenance (paper / practitioner / book) and a transparent claim,
            mechanism, and known failure mode. This is a research view, not a
            live-trading signal.
          </p>
        </div>
      </header>

      <Panel title="Filters" className="paper-research-filters">
        <div className="paper-research-filters-row">
          <label>
            <span>Category</span>
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value as Category | typeof ALL)}
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c === ALL ? "All categories" : CATEGORY_LABELS[c as Category]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Evidence</span>
            <select
              value={evidence}
              onChange={(e) =>
                setEvidence(e.target.value as EvidenceQuality | typeof ALL)
              }
            >
              {EVIDENCE_LEVELS.map((ev) => (
                <option key={ev} value={ev}>
                  {ev === ALL ? "Any evidence" : EVIDENCE_LABELS[ev as EvidenceQuality]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Search</span>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="name, claim, source, tag…"
            />
          </label>
          <div className="paper-research-count">
            {filtered.length} of {STRATEGY_LIBRARY.length}
          </div>
        </div>
      </Panel>

      <div className="paper-research-grid">
        <Panel title="Candidates" className="paper-research-list">
          {filtered.length === 0 ? (
            <EmptyState
              title="No matches"
              hint="Try clearing the filters or search term."
            />
          ) : (
            <ul className="paper-research-candidate-list">
              {filtered.map((c) => (
                <li
                  key={c.candidate_id}
                  className={
                    selected?.candidate_id === c.candidate_id
                      ? "selected"
                      : undefined
                  }
                >
                  <button
                    type="button"
                    onClick={() => setSelectedId(c.candidate_id)}
                    className="paper-research-candidate-btn"
                  >
                    <div className="paper-research-candidate-name">{c.name}</div>
                    <div className="paper-research-candidate-meta">
                      <span className="paper-research-cat">
                        {CATEGORY_LABELS[c.category]}
                      </span>
                      <span
                        className={`paper-research-evidence tone-${EVIDENCE_TONE[c.evidence_quality]}`}
                        title={EVIDENCE_LABELS[c.evidence_quality]}
                      >
                        {EVIDENCE_LABELS[c.evidence_quality]}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title={selected ? selected.name : "Candidate detail"}
          className="paper-research-detail"
        >
          {selected ? (
            <div className="paper-research-detail-body">
              <dl>
                <dt>Category</dt>
                <dd>{CATEGORY_LABELS[selected.category]}</dd>
                <dt>Evidence</dt>
                <dd>
                  <span
                    className={`paper-research-evidence tone-${EVIDENCE_TONE[selected.evidence_quality]}`}
                  >
                    {EVIDENCE_LABELS[selected.evidence_quality]}
                  </span>
                </dd>
                <dt>Market</dt>
                <dd>{selected.market}</dd>
                <dt>Timeframe</dt>
                <dd>{selected.timeframe_hint}</dd>
                <dt>Source</dt>
                <dd>{selected.source}</dd>
                <dt>Claim</dt>
                <dd>{selected.claim}</dd>
                <dt>Mechanism</dt>
                <dd>{selected.mechanism}</dd>
                <dt>Assumptions</dt>
                <dd>
                  {selected.assumptions.length ? (
                    <ul>
                      {selected.assumptions.map((a) => (
                        <li key={a}>{a}</li>
                      ))}
                    </ul>
                  ) : (
                    <em>(none specified)</em>
                  )}
                </dd>
                <dt>Risks</dt>
                <dd>
                  {selected.risks.length ? (
                    <ul>
                      {selected.risks.map((a) => (
                        <li key={a}>{a}</li>
                      ))}
                    </ul>
                  ) : (
                    <em>(none specified)</em>
                  )}
                </dd>
                <dt>Known failure modes</dt>
                <dd>
                  {selected.known_failure_modes.length ? (
                    <ul>
                      {selected.known_failure_modes.map((a) => (
                        <li key={a}>{a}</li>
                      ))}
                    </ul>
                  ) : (
                    <em>(none specified)</em>
                  )}
                </dd>
                <dt>Indicators</dt>
                <dd>
                  {selected.indicators.length ? (
                    <code>{selected.indicators.join(", ")}</code>
                  ) : (
                    <em>(none specified)</em>
                  )}
                </dd>
                <dt>Research notes</dt>
                <dd>{selected.research_notes || "—"}</dd>
                <dt>Tags</dt>
                <dd>
                  {selected.tags.map((t) => (
                    <span key={t} className="paper-research-tag">
                      {t}
                    </span>
                  ))}
                </dd>
              </dl>
              <p className="paper-research-disclaimer">
                Research view only. A candidate here has been <em>sourced</em> and
                <em> specified</em>; it is not yet backtested or paper-eligible.
                Run the research engine to promote a candidate through the
                lifecycle (backtested → validated → paper-eligible).
              </p>
            </div>
          ) : (
            <EmptyState
              title="No candidate selected"
              hint="Pick a candidate on the left to inspect its source, claim, and risks."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}
