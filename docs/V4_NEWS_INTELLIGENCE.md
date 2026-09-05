# V4 News Intelligence

Status: implemented, offline-deterministic. **News feeds provide information
availability, not guaranteed real-time execution-grade market data.**

No paid API, no API key, no network access is required for the core system or
tests. `NEWS_ENABLED=false` (default) keeps the entire trading system
functional with news disabled. The live FYERS/broker connection remains
expired and is NOT restored by V4.

## 1. Architecture

```
NewsProvider (RSS / Atom / future APIs)
  ↓  fetch (never raises; timeout, size caps, invalid-URL guard)
NewsNormalizer   → NewsEventV4 (deterministic event_id, tz-aware timestamps,
  ↓                published_at ≠ discovered_at)
NewsDeduplicator → canonical_event_id + supporting_sources[] + novelty
  ↓
EntityResolver   → NSE ticker / company / sector / index; unknown stays
  ↓                UNRESOLVED (never guessed)
EventClassifier  → 36-type vocabulary (rbi_decision, order_win, buyback, …);
  ↓                unmatched → other (never forced)
SentimentEngine  → POSITIVE/NEGATIVE/NEUTRAL/MIXED/UNKNOWN + score + confidence
  ↓                (lexicon + negation; no hits → UNKNOWN, never fake neutral)
ImpactClassifier → SEPARATE axis: impact_score/level + affected_assets
  ↓
RelevanceScorer  → company/sector/index/market relevance per target
  ↓
NewsContextResult → EvidenceLedgerV2 (NEWS category) → confidence
```

## 2. Providers & configuration

- `RssNewsProvider` — stdlib only (`urllib` + `xml.etree`); RSS 2.0 + Atom;
  malformed XML/oversized entries/invalid URLs/missing fields degrade to
  fewer items, never an exception.
- `DEFAULT_FEEDS` — five **configurable** NSE corporate RSS endpoints
  (announcements, financial results, board meetings, corporate actions,
  circulars) with `source_reliability=0.95`. URLs are configuration, not
  assumptions; edit `FeedConfig` entries if NSE relocates them.
- `GoogleNewsSearchProvider` — OPTIONAL generic search feeds
  (reliability capped at 0.6). Never the only source.
- `NewsPollingService` — dev-scale polling: configurable interval, per-feed
  timeout, retries with exponential backoff, max retries, cross-poll
  duplicate suppression, graceful `stop()`, structured logging
  (`finova.news`). Not started unless explicitly enabled.

## 3. Normalization & data contract

`NewsEventV4` fields: event_id, source, source_url, publisher, title,
description (snippet-level only; full article text is NOT stored),
published_at, discovered_at, language, country, tickers, company_names,
sectors, indices, topics, event_type, sentiment, sentiment_score,
sentiment_confidence, relevance_score, impact_score, impact_level,
affected_assets, novelty_score, source_reliability, market_horizon,
data_quality, raw_metadata, canonical_event_id, supporting_sources.
Missing publication time → `data_quality=DEGRADED`; naive timestamps are
assumed UTC (documented).

## 4. Deduplication

Deterministic: title-token Jaccard ≥ 0.6 AND publication times within 24h
AND (shared entity OR same publisher). 20 copies of one event become ONE
canonical event with `supporting_sources[]`; the first report has
`novelty=1.0`, repeats decay `1/(1+k)`. Unrelated events are never merged.

## 5. Sentiment vs impact

Two separate axes. "Record profit but misses expectations" → `MIXED`
sentiment; impact is classified per event type (RBI decision = HIGH with
affected assets BANKS/BANKNIFTY/NIFTY50/INR; executive appointment = LOW).
A positive article never directly forces a trade — it becomes evidence only.

## 6. Evidence integration & freshness

`news_to_evidence` adds NEWS-category items with honest states:
SUPPORTED (fresh, relevant, single-direction) / CONTRADICTORY (conflicting
coverage of the same entity) / STALE / LOW_QUALITY (degraded or
unreliable source) / PARTIAL (MIXED/UNKNOWN sentiment) / **UNAVAILABLE when
there is no news — never neutral**. Freshness tiers are configurable
(VERY_FRESH <15m, FRESH <1h, RECENT <6h, STALE <24h, OLD) with
per-event-type overrides so macro/policy events stay relevant longer than
company noise. Only SUPPORTED items contribute confidence weight.

## 7. Limitations

- Lexicon sentiment is deterministic but shallow — no ML model, no paid API.
- Entity alias table is a curated factual subset (extensible via constructor).
- RSS is headline-level; no full-text analysis.
- No historical news archive is built by this phase (replay uses
  caller-supplied event lists filtered by published_at).

## 8. Testing & operations

`tests/test_news_intelligence.py` (35 tests) covers: RSS/Atom parsing,
malformed XML, duplicate URLs, oversized titles, invalid URLs, missing/naive
timestamps, deterministic IDs, dedup merge/novelty, entity resolution
(RELIANCE/SBIN/BANKNIFTY + UNRESOLVED), the classification vocabulary,
sentiment (incl. MIXED and negation), impact vs sentiment separation,
relevance gating, freshness tiers + macro overrides, conflict detection,
unavailable-not-neutral evidence, provider failure, cross-poll dedupe,
graceful shutdown, and the NEWS_ENABLED gate.

Security/reliability posture: never crash on external data, 2 MB feed cap,
512/1024-char field caps, control-character stripping, URL scheme checks,
all fetch failures logged and counted.

## 9. V4 quality gate mapping

| Gate | Where |
|---|---|
| RSS provider works | `RssNewsProvider.parse` + tests |
| normalization / dedup / resolution | pipeline tests |
| sentiment / classification | lexicon + rule tests |
| ledger receives news | `news_to_evidence` tests |
| conflicting → CONTRADICTORY | `TestConflictingNews` |
| unavailable ≠ neutral | `TestUnavailableNews` |
| NEWS_ENABLED=false safe | `news_enabled_from_env` default test |

