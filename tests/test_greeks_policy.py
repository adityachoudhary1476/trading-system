"""Greek availability verdicts for the Phase 0 shadow mode.

The behaviour these tests defend, in order of how badly it would bite if it
regressed:

1. ``evaluate_candidate`` never raises and never refuses a trade (D1): a
   verdict, any verdict, is always returned.
2. ``None`` means unknown, never zero (D7): an unavailable greek is not read
   as a number.
3. Availability is per requested field: missing IV still leaves a usable delta
   via the solved premium, and a missing greek does not poison the ones that
   were computed.
4. The fallback taxonomy is a closed set, and every member is reachable.
5. ``now`` is injected: same inputs plus the same ``now`` are identical (D5).
"""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.greeks import black_scholes_price, years_to_expiry
from trading_system.autonomous.greeks_policy import (
    DEFAULT_MIN_GREEKS,
    FALLBACK_BAD_OPTION_TYPE,
    FALLBACK_BAD_SPREAD,
    FALLBACK_BAD_STRIKE,
    FALLBACK_EVALUATION_ERROR,
    FALLBACK_EXPIRED,
    FALLBACK_GREEKS_NONE,
    FALLBACK_NO_QUOTE,
    FALLBACK_NO_REFERENCE_TIME,
    FALLBACK_NO_SPOT,
    FALLBACK_NO_VOLATILITY,
    FALLBACK_REASONS,
    FALLBACK_SOLVER_FAILED,
    FALLBACK_SPOT_MISMATCH,
    GreeksVerdict,
    evaluate_candidate,
    shadow_decision,
)

UTC = timezone.utc
# Derived, not hardcoded: EXPIRY must be exactly 45 days after the evaluation
# instant or the greeks quietly price a different tenor than the fixtures expect.
NOW = datetime.now(UTC).replace(hour=9, minute=30, second=0, microsecond=0)
EXPIRY = (NOW.date() + timedelta(days=45)).isoformat()
SPOT = 25000.0
YEARS = years_to_expiry(EXPIRY, now=NOW)


def _instrument(strike=SPOT, option_type="CE", key=None):
    return SimpleNamespace(
        strike=float(strike),
        option_type=option_type,
        instrument_key=key or f"NFO:SYM|{EXPIRY}|{float(strike):g}|{option_type}",
    )


def _quote(strike=SPOT, option_type="CE", *, iv=0.15, price_sigma=None,
           ltp=None, bid=None, ask=None, spot=SPOT, **extra):
    sigma = iv if price_sigma is None else price_sigma
    if ltp is None and sigma is not None:
        ltp = black_scholes_price(
            spot=spot, strike=float(strike), years_to_expiry=YEARS,
            rate=0.065, sigma=sigma, option_type=option_type,
        )
    quote = SimpleNamespace(
        ltp=ltp,
        bid=bid if bid is not None else max(0.0, (ltp or 0.0) - 0.5),
        ask=ask if ask is not None else (ltp or 0.0) + 0.5,
        option_type=option_type,
        # Provider-native percent, as Upstox sends it.
        bid_iv=iv * 100.0 if iv is not None else None,
        ask_iv=iv * 100.0 if iv is not None else None,
        implied_vol=None,
        instrument_key=f"NSE_INDEX:NIFTY{option_type}{float(strike):g}",
    )
    for name, value in extra.items():
        setattr(quote, name, value)
    return quote


def _chain(spot=SPOT, expiry=EXPIRY):
    return SimpleNamespace(expiry=expiry, spot_price=spot)


def _evaluate(**overrides):
    args = dict(
        instrument=_instrument(),
        quote=_quote(),
        chain=_chain(),
        spot=SPOT,
        now=NOW,
    )
    args.update(overrides)
    return evaluate_candidate(**args)


def _fixture_for(reason):
    if reason == FALLBACK_NO_QUOTE:
        return {"quote": None}
    if reason == FALLBACK_BAD_STRIKE:
        return {"instrument": _instrument(strike=0.0)}
    if reason == FALLBACK_BAD_OPTION_TYPE:
        return {"instrument": _instrument(option_type="GG")}
    if reason == FALLBACK_NO_SPOT:
        return {"chain": _chain(spot=0.0)}
    if reason == FALLBACK_SPOT_MISMATCH:
        return {"spot": 20000.0}
    if reason == FALLBACK_NO_REFERENCE_TIME:
        return {"now": None}
    if reason == FALLBACK_EXPIRED:
        return {"chain": _chain(expiry=(NOW.date() - timedelta(days=1)).isoformat())}
    if reason == FALLBACK_NO_VOLATILITY:
        return {"quote": _quote(iv=None, ltp=0.0, bid=0.0, ask=0.0)}
    if reason == FALLBACK_BAD_SPREAD:
        return {"quote": _quote(iv=None, ltp=None, bid=5.0, ask=5.0)}
    if reason == FALLBACK_SOLVER_FAILED:
        return {"quote": _quote(iv=None, ltp=90.5, bid=90.0, ask=91.0)}
    if reason == FALLBACK_GREEKS_NONE:
        return {"min_greeks": ("delta", "lambda")}
    if reason == FALLBACK_EVALUATION_ERROR:
        return {"rate": "oops"}
    raise AssertionError(f"no fixture wired for {reason!r}")


class TestHealthyQuote:
    def test_quote_iv_yields_usable_delta(self):
        verdict = _evaluate()
        assert verdict.available is True
        assert verdict.fallback_reason is None
        assert verdict.missing == ()
        assert verdict.iv_source == "quote"
        assert verdict.greeks is not None
        assert 0.45 <= verdict.greeks.delta <= 0.60
        assert verdict.greeks.implied_vol == pytest.approx(0.15)
        assert verdict.instrument_key.startswith("NFO:")

    def test_atm_call_delta_is_around_one_half(self):
        verdict = _evaluate()
        assert verdict.greeks.delta == pytest.approx(0.53, abs=0.06)

    def test_put_delta_is_negative(self):
        verdict = _evaluate(
            instrument=_instrument(option_type="PE"),
            quote=_quote(option_type="PE"),
        )
        assert verdict.available is True
        assert verdict.greeks.delta < 0
        assert verdict.iv_source == "quote"

    def test_quote_iv_is_preferred_over_solving(self):
        # A premium implying a wildly different vol than the quote's own IV.
        quote = _quote(ltp=5.0)
        verdict = _evaluate(quote=quote)
        assert verdict.iv_source == "quote"
        assert verdict.greeks.implied_vol == pytest.approx(0.15)


class TestSolvedIv:
    def test_iv_recovered_from_premium_when_quote_omits_it(self):
        quote = _quote(iv=None, price_sigma=0.15)
        verdict = _evaluate(quote=quote)
        assert verdict.available is True
        assert verdict.iv_source == "solved"
        assert verdict.greeks.implied_vol == pytest.approx(0.15, abs=1e-4)
        assert 0.45 <= verdict.greeks.delta <= 0.60

    def test_bid_ask_mid_used_when_there_is_no_trade(self):
        quote = _quote(iv=None, ltp=None, bid=628.0, ask=629.0)
        verdict = _evaluate(quote=quote)
        assert verdict.iv_source == "solved"
        assert 0.45 <= verdict.greeks.delta <= 0.60


class TestFallbackReasons:
    @pytest.mark.parametrize("reason", FALLBACK_REASONS)
    def test_every_reason_is_reachable_by_a_fixture(self, reason):
        verdict = _evaluate(**_fixture_for(reason))
        assert verdict.available is False
        assert verdict.fallback_reason == reason

    def test_fallback_reasons_is_a_closed_unique_set(self):
        assert len(set(FALLBACK_REASONS)) == len(FALLBACK_REASONS)
        assert FALLBACK_REASONS == tuple(dict.fromkeys(FALLBACK_REASONS))

    def test_every_reason_has_a_wired_fixture(self):
        for reason in FALLBACK_REASONS:
            params = _fixture_for(reason)
            assert isinstance(params, dict) and params


class TestFailOpen:
    def test_none_quote_returns_a_verdict_not_an_exception(self):
        verdict = _evaluate(quote=None)
        assert isinstance(verdict, GreeksVerdict)
        assert verdict.available is False
        assert verdict.fallback_reason == FALLBACK_NO_QUOTE

    def test_empty_instrument_returns_a_verdict_not_an_exception(self):
        verdict = _evaluate(instrument=SimpleNamespace())
        assert isinstance(verdict, GreeksVerdict)
        assert verdict.available is False
        assert verdict.fallback_reason == FALLBACK_BAD_STRIKE

    def test_none_instrument_returns_a_verdict_not_an_exception(self):
        verdict = _evaluate(instrument=None)
        assert isinstance(verdict, GreeksVerdict)
        assert verdict.available is False

    def test_available_false_never_contains_a_refusal(self):
        verdict = _evaluate(quote=None)
        shadow = shadow_decision(verdict, {})
        assert verdict.available is False
        assert "refuse" not in shadow
        assert "reject" not in shadow
        assert "block" not in shadow
        assert shadow["would_fallback_to_legacy"] is True
        # The verdict itself has no action/refusal field at all.
        assert not hasattr(verdict, "action")


class TestPerFieldIndependence:
    def test_default_min_greeks_is_delta(self):
        assert DEFAULT_MIN_GREEKS == ("delta",)
        verdict = _evaluate()
        assert verdict.available is True

    def test_gamma_may_be_requested_alongside_delta(self):
        verdict = _evaluate(min_greeks=("delta", "gamma"))
        assert verdict.available is True
        assert verdict.greeks.gamma is not None

    def test_a_missing_greek_does_not_disable_a_working_delta(self):
        # The engine computes delta and gamma but not "lambda". Asking for it
        # must isolate the loss: the verdict blames only that field, and the
        # computed greeks survive untouched (never coerced to 0.0).
        healthy = _evaluate()
        demanding = _evaluate(min_greeks=("delta", "lambda"))
        assert demanding.available is False
        assert demanding.missing == ("lambda",)
        assert demanding.fallback_reason == FALLBACK_GREEKS_NONE
        assert demanding.greeks is not None
        assert demanding.greeks.delta == healthy.greeks.delta
        assert demanding.greeks.gamma == healthy.greeks.gamma
        # A delta-only consumer still gets full availability.
        assert _evaluate(min_greeks=("delta",)).available is True


class TestNoneNeverZeroed:
    def test_solver_failure_leaves_greeks_none_not_zero(self):
        verdict = _evaluate(quote=_quote(iv=None, ltp=90.5, bid=90.0, ask=91.0))
        assert verdict.greeks is None
        assert verdict.iv_source is None
        assert verdict.available is False
        assert verdict.fallback_reason == FALLBACK_SOLVER_FAILED

    def test_no_volatility_verdict_carries_no_placeholder_greeks(self):
        quote = _quote(iv=None, ltp=0.0, bid=0.0, ask=0.0)
        verdict = _evaluate(quote=quote)
        assert verdict.greeks is None
        assert verdict.iv_source is None
        assert verdict.fallback_reason == FALLBACK_NO_VOLATILITY

    def test_recovered_volatility_is_a_real_number(self):
        verdict = _evaluate(quote=_quote(iv=None, price_sigma=0.15))
        assert verdict.greeks.implied_vol == pytest.approx(0.15, abs=1e-4)
        assert verdict.greeks.delta is not None
        assert verdict.greeks.delta != 0.0


class TestDeterminism:
    def test_same_inputs_and_same_now_are_identical(self):
        first = _evaluate()
        second = _evaluate()
        assert first == second
        assert first.greeks == second.greeks
        assert first.missing == second.missing

    def test_now_after_expiry_flips_the_verdict_to_unavailable(self):
        verdict = _evaluate()
        assert verdict.available is True
        late = datetime.combine(
            date.fromisoformat(EXPIRY) + timedelta(days=3), time(9, 30), tzinfo=UTC
        )
        expired = _evaluate(now=late)
        assert expired.available is False
        assert expired.fallback_reason == FALLBACK_EXPIRED
        assert expired.greeks is None


class TestMissingFieldNames:
    def test_missing_lists_the_absent_inputs(self):
        assert _evaluate(quote=None).missing == ("quote",)
        assert _evaluate(chain=_chain(spot=0.0)).missing == ("chain_spot",)
        assert _evaluate(now=None).missing == ("now",)
        past = (NOW.date() - timedelta(days=1)).isoformat()
        assert _evaluate(chain=_chain(expiry=past)).missing == ("expiry",)
        assert _evaluate(
            quote=_quote(iv=None, ltp=0.0, bid=0.0, ask=0.0)
        ).missing == ("implied_volatility", "premium")

    def test_healthy_verdict_has_no_missing_fields(self):
        assert _evaluate().missing == ()


class TestShadowDecision:
    def test_healthy_verdict_describes_what_would_happen(self):
        verdict = _evaluate()
        shadow = shadow_decision(verdict, {})
        assert shadow["phase"] == 0
        assert shadow["shadow"] is True
        assert shadow["greeks_available"] is True
        assert shadow["iv_source"] == "quote"
        assert shadow["fallback_reason"] is None
        assert shadow["delta"] is not None
        assert shadow["phase2_target_delta_would_run"] is True
        assert shadow["phase3_risk_sizing_would_run"] is True
        assert shadow["would_fallback_to_legacy"] is False

    def test_failed_verdict_reports_the_reason(self):
        verdict = _evaluate(quote=None)
        shadow = shadow_decision(verdict, {})
        assert shadow["greeks_available"] is False
        assert shadow["fallback_reason"] == FALLBACK_NO_QUOTE
        assert shadow["delta"] is None
        assert shadow["would_fallback_to_legacy"] is True

    def test_shadow_decision_never_mutates_the_verdict(self):
        verdict = _evaluate()
        before = (verdict.available, verdict.fallback_reason, verdict.iv_source,
                  verdict.missing)
        shadow_decision(verdict, {"debug": True})
        after = (verdict.available, verdict.fallback_reason, verdict.iv_source,
                 verdict.missing)
        assert after == before