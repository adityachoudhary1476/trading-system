"""Tests for the curated strategy research library (Phase 19)."""
import pytest

from trading_system.research.strategy_library import (
    Category,
    EvidenceQuality,
    Market,
    Candidate,
    StrategyLibrary,
    DEFAULT_LIBRARY,
)


def test_default_library_non_empty():
    assert len(DEFAULT_LIBRARY) > 0


def test_default_library_covers_required_categories():
    """All required methodological families are represented."""
    for cat in (
        Category.TREND_FOLLOWING,
        Category.MOMENTUM,
        Category.MEAN_REVERSION,
        Category.BREAKOUT,
        Category.VOLATILITY,
        Category.MARKET_REGIME,
        Category.MULTI_FACTOR,
    ):
        members = DEFAULT_LIBRARY.by_category(cat)
        assert members, f"category {cat.value} has no entries"
        # Every category should have at least one PEER_REVIEWED or
        # REPUTABLE_PRACTITIONER entry (not just blog/marketing).
        assert any(
            c.evidence_quality
            in (EvidenceQuality.PEER_REVIEWED, EvidenceQuality.REPUTABLE_PRACTITIONER)
            for c in members
        ), f"category {cat.value} has only blog/marketing evidence"


def test_every_candidate_has_provenance():
    """No candidate may be anonymous."""
    for c in DEFAULT_LIBRARY.all():
        assert c.source, f"candidate {c.candidate_id} missing source"
        assert c.source_type, f"candidate {c.candidate_id} missing source_type"
        assert c.claim, f"candidate {c.candidate_id} missing claim"
        assert c.mechanism, f"candidate {c.candidate_id} missing mechanism"
        assert c.evidence_quality != EvidenceQuality.UNKNOWN, (
            f"candidate {c.candidate_id} has unknown evidence quality"
        )


def test_build_spec_routes_through_validation():
    """Every candidate builds a valid StrategySpec via the validation choke point."""
    for c in DEFAULT_LIBRARY.all():
        spec = c.build_spec(symbol="NSE:SBIN", timeframe="1d")
        # The built spec may use either the candidate's own name (local
        # builders) or the provider's own name (provider-sourced). Both
        # must be non-empty.
        assert spec.name
        assert spec.symbol == "NSE:SBIN"
        assert spec.timeframe == "1d"
        assert spec.generated_by.startswith("library:")
        # Indicators must be supported by the DSL.
        for ind in spec.indicators:
            assert ind.name is not None


def test_invalid_spec_payload_is_rejected():
    """Candidate with malformed spec must fail at validation, not crash later."""
    bad = Candidate(
        candidate_id="bad",
        name="Bad spec",
        description="should never validate",
        category=Category.TREND_FOLLOWING,
        market=Market.NSE_EQUITY,
        source="x",
        source_type="x",
        claim="x",
        mechanism="x",
        evidence_quality=EvidenceQuality.BLOG_OR_MARKETING,
        spec_builder=lambda symbol, timeframe: {
            "name": "bad",
            "symbol": symbol,
            "timeframe": timeframe,
            "indicators": [{"name": "totally_made_up", "params": {}}],
            "entry": {"type": "comparison", "left": {"kind": "field", "field": "close"}, "op": ">", "right": {"kind": "field", "field": "open"}},
        },
    )
    with pytest.raises(Exception):
        bad.build_spec(symbol="NSE:SBIN", timeframe="1d")


def test_library_lookup_by_id():
    cand = DEFAULT_LIBRARY.get("trend-ema-cross-12-26")
    assert cand.name == "EMA cross 12-26 (long-only)"
    assert cand.category == Category.TREND_FOLLOWING


def test_library_unknown_id_raises():
    with pytest.raises(KeyError):
        DEFAULT_LIBRARY.get("definitely-not-a-real-candidate")


def test_library_to_records_serializes_cleanly():
    records = DEFAULT_LIBRARY.to_records()
    assert len(records) == len(DEFAULT_LIBRARY)
    for r in records:
        assert "candidate_id" in r
        assert "category" in r
        assert "evidence_quality" in r
        # No spec_builder or any non-serializable object.
        for v in r.values():
            assert not callable(v)


def test_category_enum_values_stable():
    """Category enum values are part of the public surface."""
    assert Category.TREND_FOLLOWING.value == "trend_following"
    assert Category.BREAKOUT.value == "breakout"


def test_evidence_quality_enum_values_stable():
    assert EvidenceQuality.PEER_REVIEWED.value == "peer_reviewed"


def test_market_enum_values_stable():
    assert Market.NSE_EQUITY.value == "nse_equity"
    assert Market.NSE_INDEX.value == "nse_index"
    assert Market.INDIAN_GENERIC.value == "indian_generic"


def test_donchian_candidate_builds_valid_spec():
    cand = DEFAULT_LIBRARY.get("breakout-donchian-20")
    spec = cand.build_spec(symbol="NSE:SBIN", timeframe="1d")
    assert spec.indicators  # non-empty
    assert spec.risk.stop_loss_pct is not None


def test_no_duplicate_candidate_ids():
    ids = DEFAULT_LIBRARY.candidate_ids
    assert len(ids) == len(set(ids))


def test_library_supports_custom_subset():
    custom = StrategyLibrary(
        {cid: DEFAULT_LIBRARY.get(cid) for cid in ("trend-ema-cross-12-26",)}
    )
    assert len(custom) == 1
    assert "trend-ema-cross-12-26" in custom


def test_search_count_default_is_one():
    """search_count defaults to 1 in RobustnessEvaluationConfig."""
    from trading_system.research.strategy_lab.research_artifact import (
        RobustnessEvaluationConfig,
    )
    cfg = RobustnessEvaluationConfig()
    assert cfg.search_count == 1
    assert cfg.search_count >= 1


def test_india_targeting_present_in_library():
    """Library is India-targeted (NSE/Indian markets)."""
    for c in DEFAULT_LIBRARY.all():
        assert c.market in (Market.NSE_EQUITY, Market.NSE_INDEX, Market.INDIAN_GENERIC)