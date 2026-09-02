// Static snapshot of the curated strategy research library (Phase 19).
//
// The Python DEFAULT_LIBRARY in src/trading_system/research/strategy_library.py
// is the source of truth. The JSON file at strategyLibrary.json is generated
// from `StrategyLibrary.to_records()` and committed alongside the frontend so
// that:
//   * the page renders without any backend
//   * the catalog and its provenance are visible to humans
//
// To refresh: run from src/:
//   python -c "import json; from trading_system.research import DEFAULT_LIBRARY; \
//     print(json.dumps(DEFAULT_LIBRARY.to_records(), default=str, indent=2))" \
//     > ../frontend/src/data/strategyLibrary.json

export type EvidenceQuality =
  | "peer_reviewed"
  | "reputable_practitioner"
  | "blog_or_marketing"
  | "unknown";

export type Category =
  | "trend_following"
  | "momentum"
  | "mean_reversion"
  | "breakout"
  | "volatility"
  | "market_regime"
  | "multi_factor"
  | "price_volume";

export type Market = "nse_equity" | "nse_index" | "indian_generic";

export interface StrategyLibraryEntry {
  candidate_id: string;
  name: string;
  category: Category;
  market: Market;
  source: string;
  source_type: string;
  evidence_quality: EvidenceQuality;
  timeframe_hint: string;
  indicators: string[];
  tags: string[];
  claim: string;
  mechanism: string;
  assumptions: string[];
  risks: string[];
  known_failure_modes: string[];
  research_notes: string;
}

import raw from "./strategyLibrary.json";

export const STRATEGY_LIBRARY: StrategyLibraryEntry[] = raw as StrategyLibraryEntry[];

export const CATEGORY_LABELS: Record<Category, string> = {
  trend_following: "Trend following",
  momentum: "Momentum",
  mean_reversion: "Mean reversion",
  breakout: "Breakout",
  volatility: "Volatility",
  market_regime: "Market regime",
  multi_factor: "Multi-factor",
  price_volume: "Price/Volume",
};

export const EVIDENCE_LABELS: Record<EvidenceQuality, string> = {
  peer_reviewed: "Peer-reviewed",
  reputable_practitioner: "Reputable practitioner",
  blog_or_marketing: "Blog / Marketing",
  unknown: "Unknown",
};

export const EVIDENCE_TONE: Record<EvidenceQuality, "ok" | "warn" | "bad"> = {
  peer_reviewed: "ok",
  reputable_practitioner: "ok",
  blog_or_marketing: "warn",
  unknown: "bad",
};
