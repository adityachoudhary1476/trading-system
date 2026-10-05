"""Black-Scholes greeks tests.

The greeks module is pure maths with no I/O, so these tests are closed-form:
put-call parity, textbook reference values, monotonicity, and the
fail-closed contract on every invalid input domain.

The fail-closed cases matter more than the arithmetic. A risk rule that
silently treats an unknown delta as "no opinion" would let a position be held
because the maths could not run, which is precisely the class of bug the
portfolio's exit ordering exists to prevent. PAPER-only.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_system.autonomous.greeks import (
    DEFAULT_RISK_FREE_RATE,
    black_scholes_price,
    calculate_greeks,
    greeks_from_market_inputs,
    implied_vol_from_price,
    normalise_option_type,
    years_to_expiry,
)

UTC = timezone.utc

# Textbook reference: S=100, K=100, r=0.05, sigma=0.20, T=1y.
# Call = 10.4506, Put = 5.5735, delta(call) = 0.6368.
REF_SPOT = 100.0
REF_STRIKE = 100.0
REF_RATE = 0.05
REF_SIGMA = 0.20
REF_YEARS = 1.0


class TestNormaliseOptionType:
    def test_accepts_upstox_and_book_spellings(self):
        assert normalise_option_type("CE") == "CE"
        assert normalise_option_type("C") == "CE"
        assert normalise_option_type("call") == "CE"
        assert normalise_option_type("PE") == "PE"
        assert normalise_option_type("p") == "PE"
        assert normalise_option_type(" put ") == "PE"

    def test_unknown_or_missing_is_none(self):
        assert normalise_option_type(None) is None
        assert normalise_option_type("") is None
        assert normalise_option_type("FUT") is None
        assert normalise_option_type("SPOT") is None


class TestBlackScholesPrice:
    def test_call_matches_reference_value(self):
        price = black_scholes_price(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert price == pytest.approx(10.4506, abs=1e-3)

    def test_put_matches_reference_value(self):
        price = black_scholes_price(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert price == pytest.approx(5.5735, abs=1e-3)

    def test_put_call_parity_holds(self):
        """C - P == S - K*exp(-rT). A parity breach would mean the greeks
        derived from these prices are internally inconsistent."""
        for spot in (80.0, 100.0, 120.0):
            for strike in (90.0, 100.0, 110.0):
                call = black_scholes_price(
                    spot=spot,
                    strike=strike,
                    years_to_expiry=REF_YEARS,
                    rate=REF_RATE,
                    sigma=REF_SIGMA,
                    option_type="CE",
                )
                put = black_scholes_price(
                    spot=spot,
                    strike=strike,
                    years_to_expiry=REF_YEARS,
                    rate=REF_RATE,
                    sigma=REF_SIGMA,
                    option_type="PE",
                )
                parity = spot - strike * math.exp(-REF_RATE * REF_YEARS)
                assert call - put == pytest.approx(parity, abs=1e-8)

    def test_price_is_monotonic_in_vol(self):
        prices = [
            black_scholes_price(
                spot=REF_SPOT,
                strike=REF_STRIKE,
                years_to_expiry=REF_YEARS,
                rate=REF_RATE,
                sigma=sigma,
                option_type="CE",
            )
            for sigma in (0.10, 0.20, 0.30, 0.40)
        ]
        assert prices == sorted(prices)

    def test_deep_itm_call_converges_to_intrinsic(self):
        price = black_scholes_price(
            spot=200.0,
            strike=100.0,
            years_to_expiry=REF_YEARS,
            rate=0.0,
            sigma=0.20,
            option_type="CE",
        )
        assert price == pytest.approx(100.0, abs=0.5)

    def test_otm_option_is_cheap_but_positive(self):
        price = black_scholes_price(
            spot=100.0,
            strike=180.0,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert 0.0 < price < 1.0

    def test_returns_none_on_invalid_domain(self):
        base = dict(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert black_scholes_price(**{**base, "spot": 0.0}) is None
        assert black_scholes_price(**{**base, "spot": -1.0}) is None
        assert black_scholes_price(**{**base, "strike": 0.0}) is None
        assert black_scholes_price(**{**base, "sigma": 0.0}) is None
        assert black_scholes_price(**{**base, "sigma": -0.2}) is None
        assert black_scholes_price(**{**base, "years_to_expiry": 0.0}) is None
        assert black_scholes_price(**{**base, "option_type": "FUT"}) is None
        assert black_scholes_price(**{**base, "option_type": None}) is None
        assert black_scholes_price(**{**base, "spot": math.nan}) is None
        assert black_scholes_price(**{**base, "spot": math.inf}) is None


class TestCalculateGreeks:
    def test_call_delta_matches_reference(self):
        greeks = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert greeks is not None
        assert greeks.delta == pytest.approx(0.6368, abs=1e-3)

    def test_call_and_put_deltas_differ_by_one(self):
        call = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        put = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert call is not None and put is not None
        assert call.delta - put.delta == pytest.approx(1.0, abs=1e-9)

    def test_delta_is_negative_for_long_put(self):
        put = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert put is not None
        assert put.delta < 0.0

    def test_delta_bounds_respected(self):
        for spot in (50.0, 100.0, 150.0):
            for kind in ("CE", "PE"):
                greeks = calculate_greeks(
                    spot=spot,
                    strike=100.0,
                    years_to_expiry=REF_YEARS,
                    rate=REF_RATE,
                    sigma=REF_SIGMA,
                    option_type=kind,
                )
                assert greeks is not None
                assert -1.0 <= greeks.delta <= 1.0

    def test_gamma_is_positive_and_shared_by_call_and_put(self):
        call = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        put = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert call is not None and put is not None
        assert call.gamma > 0.0
        assert call.gamma == pytest.approx(put.gamma, abs=1e-12)

    def test_vega_is_shared_by_call_and_put(self):
        call = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        put = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert call is not None and put is not None
        assert call.vega == pytest.approx(put.vega, abs=1e-12)
        assert call.vega > 0.0

    def test_theta_is_negative_for_long_premium(self):
        call = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        put = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="PE",
        )
        assert call is not None and put is not None
        assert call.theta < 0.0
        assert put.theta < 0.0

    def test_gamma_peaks_near_the_money(self):
        atm = calculate_greeks(
            spot=100.0, strike=100.0, years_to_expiry=1.0, sigma=0.20, option_type="CE"
        )
        wing = calculate_greeks(
            spot=100.0, strike=150.0, years_to_expiry=1.0, sigma=0.20, option_type="CE"
        )
        assert atm is not None and wing is not None
        assert atm.gamma > wing.gamma

    def test_delta_falls_as_option_goes_further_otm(self):
        near = calculate_greeks(
            spot=100.0, strike=105.0, years_to_expiry=1.0, sigma=0.20, option_type="CE"
        )
        far = calculate_greeks(
            spot=100.0, strike=140.0, years_to_expiry=1.0, sigma=0.20, option_type="CE"
        )
        assert near is not None and far is not None
        assert near.delta > far.delta

    def test_snapshot_carries_price_and_vol_it_was_derived_from(self):
        greeks = calculate_greeks(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert greeks is not None
        assert greeks.implied_vol == pytest.approx(REF_SIGMA)
        assert greeks.price == pytest.approx(10.4506, abs=1e-3)

    def test_returns_none_at_expiry(self):
        """An expired contract's delta is a step function and its volatility is
        undefined, so greeks report unknown rather than false precision."""
        assert (
            calculate_greeks(
                spot=100.0, strike=90.0, years_to_expiry=0.0, sigma=0.2, option_type="CE"
            )
            is None
        )

    def test_returns_none_on_invalid_domain(self):
        base = dict(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=REF_SIGMA,
            option_type="CE",
        )
        assert calculate_greeks(**{**base, "sigma": 0.0}) is None
        assert calculate_greeks(**{**base, "spot": 0.0}) is None
        assert calculate_greeks(**{**base, "strike": -1.0}) is None
        assert calculate_greeks(**{**base, "option_type": "FUT"}) is None
        assert calculate_greeks(**{**base, "years_to_expiry": -1.0}) is None
        assert calculate_greeks(**{**base, "sigma": math.inf}) is None


class TestImpliedVolFromPrice:
    @pytest.mark.parametrize(
        "sigma,kind",
        [(0.10, "CE"), (0.20, "CE"), (0.35, "CE"), (0.10, "PE"), (0.20, "PE"), (0.35, "PE")],
    )
    def test_round_trips_through_the_pricer(self, sigma, kind):
        """The whole point of solving IV from a market price: the greeks path
        depends on this recovering the volatility that produced the price."""
        price = black_scholes_price(
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            sigma=sigma,
            option_type=kind,
        )
        assert price is not None
        solved = implied_vol_from_price(
            price=price,
            spot=REF_SPOT,
            strike=REF_STRIKE,
            years_to_expiry=REF_YEARS,
            rate=REF_RATE,
            option_type=kind,
        )
        assert solved == pytest.approx(sigma, abs=1e-4)

    def test_recovered_vol_yields_matching_greeks(self):
        price = black_scholes_price(
            spot=25000.0,
            strike=25000.0,
            years_to_expiry=0.25,
            rate=DEFAULT_RISK_FREE_RATE,
            sigma=0.14,
            option_type="CE",
        )
        assert price is not None
        solved = implied_vol_from_price(
            price=price,
            spot=25000.0,
            strike=25000.0,
            years_to_expiry=0.25,
            rate=DEFAULT_RISK_FREE_RATE,
            option_type="CE",
        )
        assert solved is not None
        greeks = calculate_greeks(
            spot=25000.0,
            strike=25000.0,
            years_to_expiry=0.25,
            rate=DEFAULT_RISK_FREE_RATE,
            sigma=solved,
            option_type="CE",
        )
        assert greeks is not None
        assert greeks.price == pytest.approx(price, abs=1e-6)
        # An ATM call's delta is only 0.5 at a zero rate. With a positive rate
        # the forward sits above spot, so delta must exceed 0.5. Pinning this
        # guards against a sign or discount-term regression in d1.
        assert greeks.delta == pytest.approx(0.6053, abs=1e-3)

    def test_atm_call_delta_exceeds_half_even_at_zero_rate(self):
        """Even with no rate, an ATM call's delta is 0.5398 rather than 0.5.

        At S=K and r=0, d1 reduces to sigma*sqrt(T)/2 = 0.1, so delta is
        N(0.1). Delta is exactly 0.5 only when d1 == 0, which at S=K would
        need a negative rate. Guards the volatility term in d1.
        """
        greeks = calculate_greeks(
            spot=100.0,
            strike=100.0,
            years_to_expiry=1.0,
            rate=0.0,
            sigma=0.20,
            option_type="CE",
        )
        assert greeks is not None
        assert greeks.delta == pytest.approx(0.5398, abs=1e-4)

    def test_delta_is_exactly_half_when_d1_is_zero(self):
        """Pinning the 0.5 delta point: choose the strike that puts d1 at zero,
        where delta == 0.5 exactly by definition of the normal CDF."""
        spot, sigma, years, rate = 100.0, 0.20, 1.0, 0.0
        # d1 = (ln(S/K) + (r + sigma^2/2)T) / (sigma*sqrt(T)) == 0
        strike = spot * math.exp((rate + 0.5 * sigma * sigma) * years)
        greeks = calculate_greeks(
            spot=spot,
            strike=strike,
            years_to_expiry=years,
            rate=rate,
            sigma=sigma,
            option_type="CE",
        )
        assert greeks is not None
        assert greeks.delta == pytest.approx(0.5, abs=1e-9)

    def test_none_when_no_time_value_remains(self):
        """A deep-ITM option pinned to intrinsic has undefined volatility."""
        assert (
            implied_vol_from_price(
                price=100.0,
                spot=200.0,
                strike=100.0,
                years_to_expiry=1.0,
                rate=0.0,
                option_type="CE",
            )
            is None
        )

    def test_none_when_price_violates_no_arbitrage_bounds(self):
        assert (
            implied_vol_from_price(
                price=150.0, spot=100.0, strike=100.0, years_to_expiry=1.0,
                option_type="CE",
            )
            is None
        )
        assert (
            implied_vol_from_price(
                price=0.01, spot=100.0, strike=50.0, years_to_expiry=1.0,
                option_type="CE", rate=0.0,
            )
            is None
        )

    def test_none_at_or_past_expiry(self):
        assert (
            implied_vol_from_price(
                price=5.0, spot=100.0, strike=100.0, years_to_expiry=0.0,
                option_type="CE",
            )
            is None
        )

    def test_none_on_invalid_inputs(self):
        base = dict(
            price=10.0, spot=100.0, strike=100.0, years_to_expiry=1.0, option_type="CE"
        )
        assert implied_vol_from_price(**{**base, "price": 0.0}) is None
        assert implied_vol_from_price(**{**base, "price": -1.0}) is None
        assert implied_vol_from_price(**{**base, "spot": 0.0}) is None
        assert implied_vol_from_price(**{**base, "strike": 0.0}) is None
        assert implied_vol_from_price(**{**base, "option_type": "FUT"}) is None
        assert implied_vol_from_price(**{**base, "price": math.nan}) is None


class TestYearsToExpiry:
    def test_parses_iso_datetime(self):
        expiry = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
        now = datetime(2026, 1, 8, 15, 30, tzinfo=UTC)
        assert years_to_expiry(expiry, now) == pytest.approx(7.0 / 365.0, abs=1e-9)

    def test_bare_date_is_end_of_day(self):
        """A bare YYYY-MM-DD covers the whole session, so from the close of the
        prior day it is nearly two days out, not one."""
        now = datetime(2026, 1, 8, 23, 59, 59, tzinfo=UTC)
        remaining = years_to_expiry("2026-01-09", now)
        assert remaining == pytest.approx(1.0 / 365.0, abs=0.01)

    def test_bare_date_parsed_as_midnight_would_differ(self):
        """Documents why the end-of-day reading matters: treating the expiry as
        midnight instead of session close shortens time by nearly a full day,
        which on a short-dated option is a materially different delta."""
        now = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
        as_session = years_to_expiry("2026-01-09", now)
        as_midnight = (
            datetime(2026, 1, 9, tzinfo=UTC) - now
        ).total_seconds() / (365.0 * 24.0 * 60.0 * 60.0)
        assert as_session > as_midnight

    def test_accepts_date_and_datetime_objects(self):
        now = datetime(2026, 1, 8, tzinfo=UTC)
        assert years_to_expiry(date(2026, 1, 9), now) is not None
        assert years_to_expiry(datetime(2026, 1, 9, tzinfo=UTC), now) is not None

    def test_expired_expiry_clamps_to_zero(self):
        now = datetime(2026, 1, 8, tzinfo=UTC)
        assert years_to_expiry("2020-01-01", now) == 0.0

    def test_naive_datetime_is_treated_as_utc(self):
        now = datetime(2026, 1, 8, tzinfo=UTC)
        naive = datetime(2026, 1, 9, 15, 30)
        aware = datetime(2026, 1, 9, 15, 30, tzinfo=UTC)
        assert years_to_expiry(naive, now) == pytest.approx(
            years_to_expiry(aware, now), abs=1e-12
        )

    def test_unparseable_expiry_is_none(self):
        now = datetime(2026, 1, 8, tzinfo=UTC)
        assert years_to_expiry(None, now) is None
        assert years_to_expiry("", now) is None
        assert years_to_expiry("not-a-date", now) is None

    def test_matches_option_expiry_shape_from_the_book(self):
        now = datetime(2026, 1, 8, 9, 30, tzinfo=UTC)
        assert years_to_expiry("2026-01-15T15:30:00+00:00", now) is not None

    def test_days_remaining_matches_wall_clock(self):
        now = datetime(2026, 1, 8, 9, 30, tzinfo=UTC)
        expiry = now + timedelta(days=3)
        assert years_to_expiry(expiry, now) == pytest.approx(3.0 / 365.0, abs=1e-9)


class TestGreeksFromMarketInputs:
    """The bridge that makes greeks affordable: it uses whatever a quote already
    carries, and recovers volatility from the premium when the payload omits IV,
    so no extra chain fetch is needed per position per sweep.
    """

    SPOT = 25000.0
    STRIKE = 25000.0
    EXPIRY = "2026-06-25"

    def _now(self):
        return datetime(2026, 1, 8, 9, 30, tzinfo=UTC)

    def _years(self):
        """The same year fraction the bridge will derive, so a synthesised
        premium is inverted against the identical expiry and the recovered
        volatility round-trips instead of drifting."""
        return years_to_expiry(self.EXPIRY, now=self._now())


    def test_uses_quote_iv_when_supplied(self):
        greeks = greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", quote_iv=0.15, now=self._now(),
        )
        assert greeks is not None
        assert greeks.implied_vol == pytest.approx(0.15)

    def test_falls_back_to_solving_vol_from_the_premium(self):
        """No IV in the payload, but the market premium is enough: invert it."""
        premium = black_scholes_price(
            spot=self.SPOT, strike=self.STRIKE, years_to_expiry=self._years(),
            rate=DEFAULT_RISK_FREE_RATE, sigma=0.16, option_type="CE",
        )
        assert premium is not None
        greeks = greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", ltp=premium, now=self._now(),
        )
        assert greeks is not None
        assert greeks.implied_vol == pytest.approx(0.16, abs=1e-3)
        # The recovered volatility must reproduce the same greeks as pricing the
        # option at that volatility directly.
        direct = calculate_greeks(
            spot=self.SPOT, strike=self.STRIKE, years_to_expiry=self._years(),
            sigma=0.16, option_type="CE",
        )
        assert direct is not None
        assert greeks.delta == pytest.approx(direct.delta, abs=1e-6)
        assert greeks.theta == pytest.approx(direct.theta, abs=1e-6)

    def test_quote_iv_is_preferred_over_the_solved_one(self):
        """When both are available the market's own IV wins: it embeds the real
        skew and liquidity, which an inversion of a single mid cannot."""
        premium = black_scholes_price(
            spot=self.SPOT, strike=self.STRIKE, years_to_expiry=self._years(),
            rate=DEFAULT_RISK_FREE_RATE, sigma=0.40, option_type="CE",
        )
        assert premium is not None
        greeks = greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", ltp=premium, quote_iv=0.15, now=self._now(),
        )
        assert greeks is not None
        assert greeks.implied_vol == pytest.approx(0.15)

    def test_none_without_any_volatility_source(self):
        assert greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", now=self._now(),
        ) is None

    def test_none_for_an_expired_contract(self):
        assert greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry="2020-01-02",
            option_type="CE", quote_iv=0.15, now=self._now(),
        ) is None

    def test_none_without_spot(self):
        assert greeks_from_market_inputs(
            spot=None, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", quote_iv=0.15, now=self._now(),
        ) is None

    def test_none_for_a_non_positive_spot(self):
        assert greeks_from_market_inputs(
            spot=0.0, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="CE", quote_iv=0.15, now=self._now(),
        ) is None

    def test_none_for_an_unparseable_expiry(self):
        assert greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry="soon",
            option_type="CE", quote_iv=0.15, now=self._now(),
        ) is None

    def test_none_for_an_unknown_option_type(self):
        assert greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="FUT", quote_iv=0.15, now=self._now(),
        ) is None

    def test_zero_quote_iv_falls_through_to_the_solver(self):
        """Upstox sends 0 for absent IV, so a zero must not be taken as a real
        volatility of zero (which has no meaning)."""
        premium = black_scholes_price(
            spot=self.SPOT, strike=self.STRIKE, years_to_expiry=self._years(),
            rate=DEFAULT_RISK_FREE_RATE, sigma=0.18, option_type="PE",
        )
        assert premium is not None
        greeks = greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="PE", ltp=premium, quote_iv=0.0, now=self._now(),
        )
        assert greeks is not None
        assert greeks.implied_vol == pytest.approx(0.18, abs=1e-3)

    def test_deep_itm_at_intrinsic_with_no_iv_is_unknown(self):
        """No time value means volatility is undefined, so the position's greeks
        are unknown rather than zero."""
        assert greeks_from_market_inputs(
            spot=25000.0, strike=20000.0, expiry=self.EXPIRY,
            option_type="CE", ltp=5000.0, now=self._now(),
        ) is None

    def test_put_delta_is_negative(self):
        premium = black_scholes_price(
            spot=self.SPOT, strike=self.STRIKE, years_to_expiry=self._years(),
            rate=DEFAULT_RISK_FREE_RATE, sigma=0.16, option_type="PE",
        )
        assert premium is not None
        greeks = greeks_from_market_inputs(
            spot=self.SPOT, strike=self.STRIKE, expiry=self.EXPIRY,
            option_type="PE", ltp=premium, now=self._now(),
        )
        assert greeks is not None
        assert greeks.delta < 0.0
