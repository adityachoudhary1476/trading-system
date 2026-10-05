"""Per-strike chain greeks and window aggregates.

The behaviour these tests defend, in order of how badly it would bite if it
regressed:

1. ``None`` means unknown, never zero. A greeks table that renders 0.00 for an
   unsolvable strike is indistinguishable from a real zero-delta strike.
2. Percent IV from the provider does not become a 1500% volatility.
3. A partial aggregate says so, rather than passing itself off as complete.
4. The window is bounded, so a request cannot turn into unbounded work.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.chain_analytics import (
    DEFAULT_WINDOW_STRIKES,
    MAX_WINDOW_STRIKES,
    analyse_chain,
)
from trading_system.autonomous.greeks import black_scholes_price, years_to_expiry

UTC = timezone.utc
# Derived, not hardcoded: EXPIRY must be exactly 45 days after the evaluation
# instant or the greeks quietly price a different tenor than the fixtures assume
# (a 0.86-year ATM delta is ~0.68, not the ~0.53 of a 45-day one).
NOW = datetime.now(UTC).replace(hour=9, minute=30, second=0, microsecond=0)
EXPIRY = (NOW.date() + timedelta(days=45)).isoformat()
SPOT = 25000.0
STRIKE_INTERVAL = 50.0
# The exact tenor the analytics module will derive. ``years_to_expiry`` treats a
# bare date as end-of-session, so this is slightly more than 45/365 — pricing the
# fixture at 45/365 would leave a gap and the recovered volatility would miss.
YEARS = years_to_expiry(EXPIRY, now=NOW)


def _quote(strike, option_type, *, iv=0.15, price_sigma=None, oi=None,
           change_oi=None, ltp=None, bid=None, ask=None, spot=SPOT):
    """A chain quote.

    ``iv`` is what the quote *reports* (None exercises the omitted-IV path).
    ``price_sigma`` is the volatility the synthetic premium was priced at, and
    defaults to ``iv`` so the normal case is a consistent quote. Set it to
    something else to make the reported IV disagree with the premium.
    """
    sigma = iv if price_sigma is None else price_sigma
    if ltp is None and sigma is not None:
        ltp = black_scholes_price(
            spot=spot, strike=strike, years_to_expiry=YEARS,
            rate=0.065, sigma=sigma, option_type=option_type,
        )
    return SimpleNamespace(
        strike=float(strike),
        bid=bid if bid is not None else max(0.0, (ltp or 0.0) - 0.5),
        ask=ask if ask is not None else (ltp or 0.0) + 0.5,
        last=ltp,
        ltp=ltp,
        open_interest=oi,
        change_oi=change_oi,
        instrument_key=f"NSE_INDEX:NIFTY{option_type}{strike:g}",
        option_type=option_type,
        # Provider-native percent, as Upstox sends it.
        bid_iv=iv * 100.0 if iv is not None else None,
        ask_iv=iv * 100.0 if iv is not None else None,
    )


def _chain(strikes, *, spot=SPOT, expiry=EXPIRY, calls=None, puts=None,
           strike_interval=STRIKE_INTERVAL):
    strikes = [float(s) for s in strikes]
    call_quotes = calls if calls is not None else {
        s: _quote(s, "CE", spot=spot) for s in strikes
    }
    put_quotes = puts if puts is not None else {
        s: _quote(s, "PE", spot=spot) for s in strikes
    }
    return SimpleNamespace(
        underlying="NIFTY", expiry=expiry, spot_price=spot,
        strike_interval=strike_interval, strikes=strikes,
        call_quotes=call_quotes, put_quotes=put_quotes,
    )


def _atms(strikes, spot=SPOT):
    return min(strikes, key=lambda s: abs(s - spot))


def _row(result, strike, option_type):
    return next(
        r for r in result.rows
        if r.strike == float(strike) and r.option_type == option_type
    )


class TestUnitsAndGreeks:
    def test_provider_percent_iv_becomes_a_sane_delta(self):
        strikes = [_atms([25000.0])]
        result = analyse_chain(_chain(strikes), now=NOW)
        row = _row(result, strikes[0], "CE")
        # 15.0 fed through as sigma would pin this at ~1.0.
        assert 0.45 <= row.delta <= 0.60
        assert row.implied_vol == pytest.approx(0.15)
        assert row.iv_source == "quote"

    def test_atm_call_delta_is_around_one_half(self):
        result = analyse_chain(_chain([25000.0]), now=NOW)
        assert _row(result, 25000.0, "CE").delta == pytest.approx(0.53, abs=0.06)

    def test_put_delta_is_negative_and_calls_positive(self):
        result = analyse_chain(_chain([25000.0]), now=NOW)
        assert _row(result, 25000.0, "CE").delta > 0
        assert _row(result, 25000.0, "PE").delta < 0

    def test_delta_decreases_away_from_the_money_on_the_call_side(self):
        strikes = [24000.0, 24500.0, 25000.0, 25500.0, 26000.0]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10)
        deltas = [_row(result, s, "CE").delta for s in strikes]
        # Rising strike puts the call further out of the money, so delta falls.
        assert deltas == sorted(deltas, reverse=True)
        assert deltas[0] > 0.8  # 1000 points ITM
        assert deltas[-1] < 0.35  # 1000 points OTM

    def test_put_delta_decreases_as_the_strike_rises(self):
        strikes = [24000.0, 24500.0, 25000.0, 25500.0, 26000.0]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10)
        deltas = [_row(result, s, "PE").delta for s in strikes]
        # A rising strike puts the put further in the money, so it goes more
        # negative: 24000 is an OTM put and 26000 a deep ITM one.
        assert deltas == sorted(deltas, reverse=True)
        assert deltas[0] > -0.2  # OTM put
        assert deltas[-1] < -0.7  # ITM put

    def test_volatility_recovered_from_premium_when_quote_omits_it(self):
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", iv=None, price_sigma=0.15)}
        result = analyse_chain(
            _chain(strikes, calls=calls), now=NOW
        )
        row = _row(result, 25000.0, "CE")
        assert row.implied_vol == pytest.approx(0.15, abs=1e-4)
        assert row.iv_source == "solved"
        assert 0.45 <= row.delta <= 0.60

    def test_quote_iv_is_preferred_over_solving(self):
        strikes = [25000.0]
        # A premium implying a wildly different vol than the quote's own IV.
        calls = {25000.0: _quote(25000.0, "CE", iv=0.15, ltp=5.0)}
        result = analyse_chain(_chain(strikes, calls=calls), now=NOW)
        row = _row(result, 25000.0, "CE")
        assert row.iv_source == "quote"
        assert row.implied_vol == pytest.approx(0.15)


class TestUnknowns:
    def test_premium_below_the_no_arbitrage_floor_yields_unknown_not_zero(self):
        """An ATM call cannot be worth less than the forward, ``S - K*e^-rT``
        (~199 for these inputs). A quote under that floor is corrupt or stale, so
        volatility is undefined and must report unknown rather than invent a
        number."""
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", iv=None, ltp=90.5,
                                 bid=90.0, ask=91.0)}
        result = analyse_chain(_chain(strikes, calls=calls), now=NOW)
        row = _row(result, 25000.0, "CE")
        assert row.ltp == pytest.approx(90.5)
        assert row.implied_vol is None
        assert row.delta is None
        assert row.iv_source is None
        assert row.has_greeks is False

    def test_expired_chain_reports_unknown_everywhere(self):
        # Derived from NOW rather than date.today(): the analyser works in UTC,
        # so a local-date call would flip between "expired" and "today" as the
        # clock crosses a boundary.
        past = (NOW.date() - timedelta(days=1)).isoformat()
        result = analyse_chain(_chain([25000.0], expiry=past), now=NOW)
        assert all(r.delta is None for r in result.rows)
        assert result.summary.rows_with_greeks == 0
        assert any("not in the future" in n for n in result.notes)

    def test_missing_spot_refuses_rather_than_emitting_empty_rows(self):
        result = analyse_chain(_chain([24000.0, 25000.0], spot=0.0), now=NOW)
        assert result.rows == []
        assert result.spot_price is None
        assert any("spot price is unavailable" in n for n in result.notes)

    def test_chain_without_strikes_is_reported(self):
        result = analyse_chain(_chain([]), now=NOW)
        assert result.rows == []
        assert any("no strikes" in n for n in result.notes)

    def test_chain_missing_expiry_is_reported(self):
        chain = _chain([25000.0], expiry="")
        result = analyse_chain(chain, now=NOW)
        assert result.rows == []
        assert any("missing an underlying or expiry" in n for n in result.notes)

    def test_unparseable_strike_values_do_not_raise(self):
        chain = SimpleNamespace(
            underlying="NIFTY", expiry=EXPIRY, spot_price="not-a-number",
            strike_interval=None, strikes=[25000.0],
            call_quotes={}, put_quotes={},
        )
        result = analyse_chain(chain, now=NOW)
        assert result.rows == []

    def test_moneyness_is_labelled_per_side(self):
        strikes = [24000.0, 25000.0, 26000.0]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10)
        assert _row(result, 24000.0, "CE").moneyness == "ITM"
        assert _row(result, 26000.0, "CE").moneyness == "OTM"
        assert _row(result, 25000.0, "CE").moneyness == "ATM"
        # Inverted for the put side, which is the whole point of having both.
        assert _row(result, 24000.0, "PE").moneyness == "OTM"
        assert _row(result, 26000.0, "PE").moneyness == "ITM"


class TestWindow:
    def test_window_defaults_to_a_symmetric_band_around_atm(self):
        strikes = [s for s in range(23000, 27001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW)
        assert result.summary.atm_strike == 25000.0
        assert result.summary.lower_strike == pytest.approx(24500.0)
        assert result.summary.upper_strike == pytest.approx(25500.0)
        assert len(result.rows) == 2 * (2 * DEFAULT_WINDOW_STRIKES + 1)

    def test_window_of_zero_is_atm_only(self):
        strikes = [s for s in range(23000, 27001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=0)
        assert {r.strike for r in result.rows} == {25000.0}

    def test_window_is_clamped_to_the_maximum(self):
        strikes = [s for s in range(20000, 30001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10_000)
        assert result.window_truncated is True
        assert result.summary.lower_strike == pytest.approx(
            25000.0 - MAX_WINDOW_STRIKES * STRIKE_INTERVAL
        )
        assert any("clamped" in n for n in result.notes)

    def test_negative_window_is_treated_as_atm_only(self):
        result = analyse_chain(_chain([25000.0]), now=NOW, window_strikes=-5)
        assert {r.strike for r in result.rows} == {25000.0}

    def test_unparseable_window_falls_back_to_the_default(self):
        strikes = [s for s in range(23000, 27001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes="wide")
        assert len(result.rows) == 2 * (2 * DEFAULT_WINDOW_STRIKES + 1)

    def test_window_is_reported_in_the_notes_when_narrowed(self):
        strikes = [s for s in range(23000, 27001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=2)
        assert any("of 81 strikes" in n for n in result.notes)

    def test_atm_is_the_nearest_strike_not_an_exact_match(self):
        strikes = [24900.0, 25100.0]
        result = analyse_chain(_chain(strikes, spot=25000.0), now=NOW)
        # Equidistant: min() keeps the lower, and the choice is explicit.
        assert result.summary.atm_strike == 24900.0


class TestAggregates:
    def test_totals_cover_every_row_in_the_window(self):
        strikes = [s for s in range(24000, 26001, 50)]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10)
        assert result.summary.rows_total == len(result.rows)
        assert result.summary.rows_with_greeks == len(result.rows)
        assert result.summary.coverage == pytest.approx(1.0)

    def test_net_delta_is_calls_plus_puts(self):
        result = analyse_chain(_chain([25000.0]), now=NOW)
        s = result.summary
        assert s.net_delta == pytest.approx(
            s.call_delta_total + s.put_delta_total
        )

    def test_net_delta_is_negative_when_puts_dominate(self):
        # An OTM call (delta ~0.1) against an ITM put (delta ~-0.7). A put is in
        # the money *above* spot, so the put needs the higher strike.
        strikes = [26000.0, 27000.0]
        calls = {27000.0: _quote(27000.0, "CE")}
        puts = {26000.0: _quote(26000.0, "PE")}
        result = analyse_chain(_chain(strikes, calls=calls, puts=puts), now=NOW)
        assert result.summary.call_delta_total > 0
        assert result.summary.put_delta_total < 0
        assert result.summary.net_delta < 0

    def test_put_call_oi_ratio(self):
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", oi=1000)}
        puts = {25000.0: _quote(25000.0, "PE", oi=1500)}
        result = analyse_chain(_chain(strikes, calls=calls, puts=puts), now=NOW)
        assert result.summary.put_call_oi_ratio == pytest.approx(1.5)

    def test_oi_ratio_is_none_when_call_oi_is_zero(self):
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", oi=0)}
        puts = {25000.0: _quote(25000.0, "PE", oi=1500)}
        result = analyse_chain(_chain(strikes, calls=calls, puts=puts), now=NOW)
        assert result.summary.put_call_oi_ratio is None

    def test_oi_and_change_oi_are_carried_through_per_row(self):
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", oi=4321, change_oi=-999)}
        result = analyse_chain(_chain(strikes, calls=calls), now=NOW)
        row = _row(result, 25000.0, "CE")
        assert row.oi == 4321
        assert row.change_oi == -999

    def test_aggregate_is_none_when_no_row_has_the_value(self):
        strikes = [25000.0]
        # No premium at all, and no reported IV: nothing to work from.
        calls = {25000.0: _quote(25000.0, "CE", iv=None, ltp=0.0,
                                 bid=0.0, ask=0.0)}
        result = analyse_chain(_chain(strikes, calls=calls, puts={}), now=NOW)
        assert result.summary.call_delta_total is None
        assert result.summary.gamma_total is None
        assert result.summary.rows_with_greeks == 0
        assert result.summary.coverage == pytest.approx(0.0)

    def test_partial_coverage_is_reported_not_hidden(self):
        """One side solvable, the other not: the net figure is arithmetically
        fine but analytically incomplete, and has to say so."""
        strikes = [25000.0]
        puts = {25000.0: _quote(25000.0, "PE", iv=None, ltp=0.0,
                                bid=0.0, ask=0.0)}
        result = analyse_chain(_chain(strikes, puts=puts), now=NOW)
        s = result.summary
        assert s.call_delta_total is not None
        assert s.put_delta_total is None
        assert s.net_delta is not None
        assert 0.0 < s.coverage < 1.0
        assert any("incomplete side" in n for n in result.notes)

    def test_coverage_is_none_with_no_rows(self):
        result = analyse_chain(_chain([], spot=0.0), now=NOW)
        assert result.summary.coverage is None

    def test_vega_and_theta_totals_are_reported(self):
        result = analyse_chain(_chain([25000.0]), now=NOW)
        assert result.summary.vega_total is not None
        # A long call bleeds time value, so theta is negative.
        assert result.summary.theta_total < 0
        assert result.summary.gamma_total > 0

    def test_one_sided_chain_does_not_raise(self):
        strikes = [24000.0, 25000.0]
        result = analyse_chain(_chain(strikes, puts={}), now=NOW)
        assert result.summary.put_delta_total is None
        assert result.summary.put_oi is None
        assert result.rows and all(r.option_type == "CE" for r in result.rows)


class TestPerformance:
    def test_worst_case_full_window_stays_within_a_request_budget(self):
        """Every row needing an implied-vol solve is the expensive path: a
        bisection per row. The window cap is what bounds it, so this pins the
        bound. The threshold is deliberately loose (an order of magnitude above
        the observed ~60ms) to catch a real regression without flaking on a
        loaded machine."""
        import time

        strikes = [20000.0 + 50 * i for i in range(201)]
        calls = {
            s: _quote(s, "CE", iv=None, price_sigma=0.15) for s in strikes
        }
        puts = {
            s: _quote(s, "PE", iv=None, price_sigma=0.15) for s in strikes
        }
        chain = _chain(strikes, calls=calls, puts=puts)

        started = time.perf_counter()
        result = analyse_chain(chain, now=NOW, window_strikes=MAX_WINDOW_STRIKES)
        elapsed = time.perf_counter() - started

        assert len(result.rows) == 402
        assert result.summary.rows_with_greeks == 402
        assert elapsed < 2.0, f"took {elapsed:.2f}s"


class TestRowsAndShape:
    def test_rows_come_back_sorted_by_strike_then_call_before_put(self):
        strikes = [25000.0, 24500.0, 25500.0]
        result = analyse_chain(_chain(strikes), now=NOW, window_strikes=10)
        assert [r.strike for r in result.rows] == [
            24500.0, 24500.0, 25000.0, 25000.0, 25500.0, 25500.0,
        ]
        assert [r.option_type for r in result.rows[:2]] == ["CE", "PE"]

    def test_a_missing_quote_on_one_side_does_not_create_a_row(self):
        strikes = [24500.0, 25000.0]
        calls = {24500.0: _quote(24500.0, "CE")}
        puts = {24500.0: _quote(24500.0, "PE"), 25000.0: _quote(25000.0, "PE")}
        result = analyse_chain(_chain(strikes, calls=calls, puts=puts), now=NOW)
        assert len(result.rows) == 3

    def test_result_carries_identity_for_the_ui(self):
        result = analyse_chain(_chain([25000.0], strike_interval=50.0), now=NOW)
        assert result.underlying == "NIFTY"
        assert result.expiry == EXPIRY
        assert result.spot_price == SPOT
        assert result.strike_interval == 50.0
        assert result.as_of == NOW.isoformat()

    def test_as_of_is_injectable_so_output_is_deterministic(self):
        a = analyse_chain(_chain([25000.0]), now=NOW)
        b = analyse_chain(_chain([25000.0]), now=NOW)
        assert a.as_of == b.as_of
        assert [r.delta for r in a.rows] == [r.delta for r in b.rows]

    def test_bid_ask_mid_is_used_when_there_is_no_trade(self):
        """Illiquid wings often have a quote but no LTP."""
        strikes = [25000.0]
        calls = {
            25000.0: _quote(25000.0, "CE", iv=None, ltp=None,
                            bid=628.0, ask=629.0)
        }
        result = analyse_chain(_chain(strikes, calls=calls), now=NOW)
        row = _row(result, 25000.0, "CE")
        assert row.ltp == pytest.approx(628.5)
        assert row.iv_source == "solved"
        assert 0.45 <= row.delta <= 0.60

    def test_no_price_at_all_leaves_greeks_unknown(self):
        strikes = [25000.0]
        calls = {25000.0: _quote(25000.0, "CE", iv=None, ltp=0.0,
                                bid=0.0, ask=0.0)}
        result = analyse_chain(_chain(strikes, calls=calls), now=NOW)
        assert _row(result, 25000.0, "CE").delta is None

    def test_strikes_are_derived_from_quotes_when_not_listed(self):
        calls = {24000.0: _quote(24000.0, "CE"), 25000.0: _quote(25000.0, "CE")}
        puts = {24000.0: _quote(24000.0, "PE"), 25000.0: _quote(25000.0, "PE")}
        chain = SimpleNamespace(
            underlying="NIFTY", expiry=EXPIRY, spot_price=SPOT,
            strike_interval=None, strikes=[], call_quotes=calls, put_quotes=puts,
        )
        result = analyse_chain(chain, now=NOW)
        assert result.summary.rows_total == 4
        assert result.summary.atm_strike == 25000.0