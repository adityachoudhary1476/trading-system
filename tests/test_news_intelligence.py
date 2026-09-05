"""V4 news intelligence tests.

SYNTHETIC_TEST fixtures only — no network access, no live news.
"""
from __future__ import annotations

import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fixtures.v4_fixtures import (  # noqa: E402
    BASE_TIME, MALFORMED_XML, EMPTY_XML, atom_payload, rss_payload,
    make_raw, positive_company_news, negative_company_news, mixed_news,
    duplicate_articles, conflicting_sources, unknown_company_news,
    missing_timestamp_news, stale_news, fresh_news, macro_event,
    sector_event, company_event, all_news_items,
)
from trading_system.research.news_intelligence import (  # noqa: E402
    FeedConfig, FreshnessTier, NewsNormalizer, NewsDeduplicator,
    NewsPipeline, NewsPollingService, NewsProvider, RssNewsProvider,
    classify_freshness, detect_conflicts, news_enabled_from_env,
    news_to_evidence, EventClassifier, SentimentEngine, ImpactClassifier,
    RelevanceScorer, EntityResolver, build_news_context,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    EvidenceAvailability, EvidenceCategory, EvidenceLedgerV2,
)
from trading_system.research.market_context import DataQualityTier  # noqa: E402

FEED = FeedConfig("test", "https://synthetic.test/feed", "announcements",
                  "TestWire", 0.9)
PROVIDER = RssNewsProvider()
NORMALIZER = NewsNormalizer()


def _normalize_all(raws):
    return [NORMALIZER.normalize(r, discovered_at=BASE_TIME) for r in raws]


class TestRssParsing:

    def test_rss20_parse_with_tz(self):
        items = PROVIDER.parse(rss_payload([
            ("Reliance wins order", "https://u1", "Fri, 04 Sep 2026 09:30:00 GMT"),
            ("Infosys results", "https://u2", "Fri, 04 Sep 2026 08:00:00 GMT"),
        ]), FEED)
        assert len(items) == 2
        assert items[0].published_raw.startswith("Fri, 04 Sep 2026")

    def test_atom_parse(self):
        items = PROVIDER.parse(atom_payload([
            ("Tata Steel update", "https://a1", "2026-09-04T09:00:00Z"),
        ]), FEED)
        assert len(items) == 1
        assert items[0].title == "Tata Steel update"

    def test_malformed_xml_degrades_gracefully(self):
        assert PROVIDER.parse(MALFORMED_XML, FEED) == []

    def test_empty_feed(self):
        assert PROVIDER.parse(EMPTY_XML, FEED) == []

    def test_duplicate_url_within_feed_dropped(self):
        items = PROVIDER.parse(rss_payload([
            ("Same title", "https://dup", "Fri, 04 Sep 2026 09:30:00 GMT"),
            ("Same title", "https://dup", "Fri, 04 Sep 2026 09:30:00 GMT"),
        ]), FEED)
        assert len(items) == 1

    def test_item_without_title_skipped(self):
        xml = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
               b"<item><link>https://x</link></item></channel></rss>")
        assert PROVIDER.parse(xml, FEED) == []

    def test_oversized_title_truncated(self):
        from trading_system.research.news_intelligence import MAX_TITLE_LEN
        items = PROVIDER.parse(rss_payload([
            ("x" * 5000, "https://big", "Fri, 04 Sep 2026 09:30:00 GMT"),
        ]), FEED)
        assert items and len(items[0].title) <= MAX_TITLE_LEN

    def test_invalid_url_never_fetched(self):
        # ftp:// is rejected before any network call; no exception.
        assert PROVIDER.fetch(FeedConfig("bad", "ftp://nope")) == []

    def test_missing_pubdate_marks_degraded(self):
        raw = make_raw("Tata Steel commissions plant", BASE_TIME)
        raw.published_raw = ""
        ev = NORMALIZER.normalize(raw, discovered_at=BASE_TIME)
        assert ev.published_at is None
        assert ev.data_quality == DataQualityTier.DEGRADED

    def test_naive_timestamp_assumed_utc(self):
        raw = make_raw("Title", BASE_TIME)
        raw.published_raw = "2026-09-04T09:00:00"
        ev = NORMALIZER.normalize(raw)
        assert ev.published_at.tzinfo is not None


class TestNormalization:

    def test_event_id_deterministic(self):
        raw = positive_company_news()
        e1 = NORMALIZER.normalize(raw, discovered_at=BASE_TIME)
        e2 = NORMALIZER.normalize(raw, discovered_at=BASE_TIME)
        assert e1.event_id == e2.event_id
        other = NORMALIZER.normalize(negative_company_news())
        assert other.event_id != e1.event_id

    def test_published_vs_discovered_separate(self):
        raw = make_raw("Title here", BASE_TIME - timedelta(hours=2))
        ev = NORMALIZER.normalize(raw, discovered_at=BASE_TIME)
        assert ev.published_at < ev.discovered_at


from datetime import timedelta  # noqa: E402

class TestDeduplication:

    def test_duplicates_merge_to_one_canonical(self):
        evs = _normalize_all(duplicate_articles())
        canonical = NewsDeduplicator().deduplicate(evs)
        assert len(canonical) == 1
        assert len(canonical[0].supporting_sources) == 2
        assert canonical[0].novelty_score == 1.0

    def test_duplicate_novelty_decays(self):
        evs = _normalize_all(duplicate_articles())
        NewsDeduplicator().deduplicate(evs)
        dups = [e for e in evs if e.canonical_event_id != e.event_id]
        assert dups and all(e.novelty_score < 1.0 for e in dups)

    def test_unrelated_events_not_merged(self):
        evs = _normalize_all([positive_company_news(), macro_event()])
        assert len(NewsDeduplicator().deduplicate(evs)) == 2


class TestEntityResolution:

    def test_reliance_maps_to_ticker(self):
        ev = NewsPipeline().run([positive_company_news()],
                                as_of=BASE_TIME).events[0]
        assert "RELIANCE" in ev.tickers
        assert "Reliance Industries" in ev.company_names

    def test_sbi_full_name_maps(self):
        raw = make_raw("State Bank of India cuts savings rate",
                       BASE_TIME - timedelta(minutes=10))
        ev = NewsPipeline().run([raw], as_of=BASE_TIME).events[0]
        assert "SBIN" in ev.tickers

    def test_nifty_bank_maps_to_index(self):
        raw = make_raw("Nifty Bank outperforms as PSU bank stocks rally",
                       BASE_TIME - timedelta(minutes=10))
        ev = NewsPipeline().run([raw], as_of=BASE_TIME).events[0]
        assert "BANKNIFTY" in ev.indices

    def test_unknown_entity_stays_unresolved(self):
        result = NewsPipeline().run([unknown_company_news()], as_of=BASE_TIME)
        assert result.events[0].tickers == []
        assert any("Zylotech" in u for u in result.unresolved_entities)

class TestEventClassification:

    def _type_of(self, raw):
        ev = NewsNormalizer().normalize(raw, discovered_at=BASE_TIME)
        return EventClassifier().classify(ev)

    def test_rbi_decision(self):
        assert self._type_of(macro_event()) == "rbi_decision"

    def test_order_win(self):
        assert self._type_of(positive_company_news()) == "order_win"

    def test_regulatory_action(self):
        # SEBI involvement classifies as sebi_action (a regulatory-action type)
        assert self._type_of(negative_company_news()) == "sebi_action"

    def test_unknown_type_is_other_not_forced(self):
        raw = make_raw("Quarterly board meeting scheduled for Friday", BASE_TIME)
        assert self._type_of(raw) == "other"

    def test_types_vocabulary_covers_spec(self):
        required = {"earnings", "earnings_guidance", "revenue_update",
                    "profit_warning", "acquisition", "merger", "demerger",
                    "order_win", "buyback", "dividend", "credit_rating",
                    "regulatory_approval", "regulatory_action", "rbi_decision",
                    "sebi_action", "macroeconomic_event", "analyst_update",
                    "other"}
        assert required.issubset(set(EventClassifier.TYPES))


class TestSentiment:

    def _sentiment_of(self, raw):
        ev = NewsNormalizer().normalize(raw, discovered_at=BASE_TIME)
        return SentimentEngine().analyze(ev)

    def test_positive(self):
        ev = self._sentiment_of(positive_company_news())
        assert ev.sentiment == "POSITIVE" and ev.sentiment_score > 0

    def test_negative(self):
        ev = self._sentiment_of(negative_company_news())
        assert ev.sentiment == "NEGATIVE" and ev.sentiment_score < 0

    def test_mixed_record_profit_misses(self):
        assert self._sentiment_of(mixed_news()).sentiment == "MIXED"

    def test_no_signal_is_unknown_never_fake_neutral(self):
        ev = self._sentiment_of(
            make_raw("Board meeting scheduled for Friday", BASE_TIME))
        assert ev.sentiment == "UNKNOWN" and ev.sentiment_score is None

    def test_negation_handled(self):
        ev = self._sentiment_of(
            make_raw("Company denies probe reports", BASE_TIME))
        assert ev.sentiment in ("UNKNOWN", "MIXED")

class TestImpact:

    def _impact_of(self, raw):
        ev = NewsNormalizer().normalize(raw, discovered_at=BASE_TIME)
        ev.event_type = EventClassifier().classify(ev)
        SentimentEngine().analyze(ev)
        return ImpactClassifier().classify(ev)

    def test_rbi_high_impact_with_assets(self):
        ev = self._impact_of(macro_event())
        assert ev.impact_level == "HIGH"
        assert "BANKNIFTY" in ev.affected_assets

    def test_appointment_low_impact(self):
        ev = self._impact_of(make_raw("Zylotech appoints new CFO", BASE_TIME))
        assert ev.impact_level == "LOW"

    def test_sentiment_and_impact_are_distinct(self):
        ev = self._impact_of(make_raw(
            "Company quarterly results show record profit "
            "but misses analyst expectations", BASE_TIME))
        assert ev.sentiment == "MIXED"
        assert ev.impact_level in ("MEDIUM", "HIGH")


class TestRelevance:

    def _relevance(self, raw, **target):
        ev = NewsNormalizer().normalize(raw, discovered_at=BASE_TIME)
        ev.event_type = EventClassifier().classify(ev)
        ev = EntityResolver().resolve(ev)
        return RelevanceScorer().score(ev, **target)

    def test_company_exact_match_high(self):
        assert self._relevance(positive_company_news(),
                               target_ticker="RELIANCE") == 1.0

    def test_other_company_low(self):
        assert self._relevance(positive_company_news(),
                               target_ticker="TCS") <= 0.3

    def test_macro_event_market_wide(self):
        assert self._relevance(macro_event(), target_ticker="TCS") >= 0.8

    def test_sector_relevance(self):
        score = self._relevance(
            make_raw("Banking sector credit growth improves", BASE_TIME),
            target_ticker="SBIN", target_sector="Banking")
        assert score >= 0.6


class TestFreshness:

    def test_tiers(self):
        assert classify_freshness(BASE_TIME - timedelta(minutes=5),
                                  BASE_TIME) == FreshnessTier.VERY_FRESH
        assert classify_freshness(BASE_TIME - timedelta(minutes=30),
                                  BASE_TIME) == FreshnessTier.FRESH
        assert classify_freshness(BASE_TIME - timedelta(hours=3),
                                  BASE_TIME) == FreshnessTier.RECENT
        assert classify_freshness(BASE_TIME - timedelta(hours=10),
                                  BASE_TIME) == FreshnessTier.STALE
        assert classify_freshness(BASE_TIME - timedelta(days=2),
                                  BASE_TIME) == FreshnessTier.OLD

    def test_missing_timestamp_unknown(self):
        assert classify_freshness(None, BASE_TIME) == FreshnessTier.UNKNOWN

    def test_macro_events_stay_relevant_longer(self):
        assert classify_freshness(BASE_TIME - timedelta(hours=30),
                                  BASE_TIME) == FreshnessTier.OLD
        assert classify_freshness(
            BASE_TIME - timedelta(hours=30), BASE_TIME,
            overrides={"rbi_decision": 48.0},
            event_type="rbi_decision") == FreshnessTier.STALE

class TestConflictingNews:

    def test_conflict_detected(self):
        result = NewsPipeline().run(conflicting_sources(), as_of=BASE_TIME)
        assert any(c["entity"] == "RELIANCE" for c in result.conflicts)

    def test_conflict_becomes_contradictory_evidence(self):
        result = NewsPipeline().run(conflicting_sources(), as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="RELIANCE")
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx, as_of=BASE_TIME)
        assert any(i.availability == EvidenceAvailability.CONTRADICTORY
                   for i in ledger.items)


class TestUnavailableNews:

    def test_no_news_is_unavailable_not_neutral(self):
        from trading_system.research.news_intelligence import NewsPipelineResult
        ctx = build_news_context(NewsPipelineResult(), as_of=BASE_TIME,
                                 target_ticker="RELIANCE")
        assert ctx.news_status == "unavailable"
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx)
        news_items = [i for i in ledger.items
                      if i.category == EvidenceCategory.NEWS]
        assert news_items
        assert all(i.availability == EvidenceAvailability.UNAVAILABLE
                   for i in news_items)

    def test_stale_news_marked_stale(self):
        result = NewsPipeline().run([stale_news()], as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="INFY")
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx, as_of=BASE_TIME)
        event_items = [i for i in ledger.items
                       if i.category == EvidenceCategory.NEWS
                       and i.signal.startswith("event:")]
        assert event_items
        assert all(i.availability == EvidenceAvailability.STALE
                   for i in event_items)


class TestProviderFailure:

    def test_raising_provider_never_crashes(self):
        class Bad(NewsProvider):
            name = "bad"

            def fetch(self, feed, timeout=10.0):
                raise RuntimeError("boom")

        svc = NewsPollingService([Bad()], [FEED], max_retries=2,
                                 backoff_base_s=0.001)
        res = svc.poll_once(as_of=BASE_TIME)
        assert res.articles_ingested == 0
        assert svc.failures.get("bad:test", 0) >= 1

    def test_polling_dedupes_across_polls(self):
        class Fake(NewsProvider):
            name = "fake"

            def __init__(self, items):
                self._items = items

            def fetch(self, feed, timeout=10.0):
                return list(self._items)

        svc = NewsPollingService([Fake(duplicate_articles())], [FEED],
                                 backoff_base_s=0.001)
        r1 = svc.poll_once(as_of=BASE_TIME)
        r2 = svc.poll_once(as_of=BASE_TIME)
        assert r1.articles_ingested > 0
        assert r2.articles_ingested == 0

    def test_graceful_stop_without_start(self):
        svc = NewsPollingService([], [FEED])
        svc.stop()  # never started; must not raise


class TestEnvironmentGate:

    def test_disabled_by_default(self):
        assert news_enabled_from_env("false") is False

    def test_full_fixture_batch_runs(self):
        res = NewsPipeline().run(all_news_items(), as_of=BASE_TIME)
        assert res.articles_ingested >= 12
        assert res.events
        assert res.duplicates_removed >= 2
