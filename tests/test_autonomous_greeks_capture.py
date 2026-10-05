"""Greeks capture during mark-to-market.

``AutonomousController._capture_position_greeks`` reads nothing from ``self``,
so it is exercised unbound here. That keeps these tests about the anchoring
rules -- which are the part with a correctness trap in them -- rather than
about assembling a whole controller.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.greeks import black_scholes_price

UTC = timezone.utc

# Far enough out that a months-old expiry is still live: a contract with no time
# value has no volatility, and greeks would legitimately be unknown.
#
# DAYS_TO_EXPIRY is the single source of truth for the horizon, and the expiry is
# dated from UTC because the capture path measures time to expiry in UTC. Mixing
# a local-dated expiry with a UTC measurement silently shifts the year fraction
# by a day, which moved a solved IV by ~20bp and read as a solver regression.
DAYS_TO_EXPIRY = 60
EXPIRY = (
    datetime.now(UTC).date() + timedelta(days=DAYS_TO_EXPIRY)
).isoformat()
SPOT = 25000.0
STRIKE = 25000.0

# ATM call delta for ~15% volatility over ~60 days, within 60 days of roll.
DELTA_MIN, DELTA_MAX = 0.45, 0.60


def _pos(**kwargs):
    """A position that accepts the greeks attributes, like the real one."""
    pos = SimpleNamespace(
        symbol="NFO:NIFTY_CE_1",
        strike=STRIKE,
        expiry=EXPIRY,
        option_type="CE",
        entry_delta=None,
        last_delta=None,
        entry_iv=None,
        last_iv=None,
        greeks_as_of=None,
    )
    age = kwargs.pop("age_seconds", 0.0)
    pos.holding_seconds = lambda now=None: age
    for key, value in kwargs.items():
        setattr(pos, key, value)
    return pos


def _quote(underlying_price=SPOT, iv=0.15):
    return SimpleNamespace(underlying_price=underlying_price, implied_vol=iv)


def _ltp(iv=0.15, spot=SPOT, strike=STRIKE, option_type="CE"):
    """A market premium consistent with *iv*, so greeks are recoverable."""
    # Uses the shared horizon so the premium is priced for exactly the expiry
    # the capture path will measure against.
    years = DAYS_TO_EXPIRY / 365.0
    price = black_scholes_price(
        spot=spot, strike=strike, years_to_expiry=years,
        rate=0.065, sigma=iv, option_type=option_type,
    )
    assert price is not None
    return price


def _capture(pos, quote, ltp, spot_price=None):
    AutonomousController._capture_position_greeks(
        None, pos, quote, ltp=ltp, spot_price=spot_price
    )
    return pos


class TestEntryAnchoring:
    def test_anchors_a_fresh_position(self):
        pos = _capture(_pos(), _quote(), _ltp())
        assert pos.entry_delta is not None
        assert DELTA_MIN <= pos.entry_delta <= DELTA_MAX
        assert pos.last_delta == pytest.approx(pos.entry_delta)
        assert pos.entry_iv == pytest.approx(0.15)
        assert pos.last_iv == pytest.approx(0.15)
        assert pos.greeks_as_of is not None

    def test_refuses_a_late_anchor_but_still_records_the_current_delta(self):
        """The trap this rule exists to prevent.

        Anchoring a 3-day-old position would measure today's delta as the
        baseline, so the very next tick would report no decay and the exit
        would never fire. Recording the current delta is still correct, it just
        cannot serve as a comparison point.
        """
        pos = _capture(_pos(age_seconds=3 * 86400.0), _quote(), _ltp())
        assert pos.entry_delta is None
        assert pos.entry_iv is None
        assert pos.last_delta is not None

    def test_anchors_at_the_window_boundary(self):
        pos = _capture(_pos(age_seconds=1800.0), _quote(), _ltp())
        assert pos.entry_delta is not None

    def test_never_overwrites_an_existing_anchor(self):
        pos = _pos(entry_delta=0.5, entry_iv=0.20)
        _capture(pos, _quote(), _ltp())
        assert pos.entry_delta == pytest.approx(0.5)
        assert pos.entry_iv == pytest.approx(0.20)

    def test_last_delta_tracks_a_collapsing_position(self):
        """The signal the exit depends on: entry anchored, then delta decays as
        the underlying moves away from the strike at constant volatility."""
        pos = _capture(_pos(), _quote(), _ltp())
        anchor = pos.entry_delta
        _capture(pos, _quote(underlying_price=24000.0), _ltp(spot=24000.0))
        assert pos.entry_delta == pytest.approx(anchor)
        assert pos.last_delta < anchor
        assert pos.last_delta > 0.0

    def test_position_without_a_holding_clock_gets_no_anchor(self):
        pos = _pos()
        del pos.holding_seconds
        _capture(pos, _quote(), _ltp())
        assert pos.entry_delta is None
        assert pos.last_delta is not None

    def test_unknown_holding_age_gets_no_anchor(self):
        pos = _pos()
        pos.holding_seconds = lambda now=None: None
        _capture(pos, _quote(), _ltp())
        assert pos.entry_delta is None
        assert pos.last_delta is not None


class TestSpotResolution:
    def test_uses_the_quote_underlying_price(self):
        pos = _capture(
            _pos(), _quote(underlying_price=25100.0), _ltp(spot=25100.0),
            spot_price=9999.0,
        )
        assert pos.entry_delta is not None

    def test_falls_back_to_the_supplied_spot(self):
        """The payload omitted the underlying price; the scheduler's observed
        market spot keeps greeks available instead of dropping them."""
        pos = _capture(_pos(), _quote(underlying_price=None), _ltp(),
                       spot_price=SPOT)
        assert pos.entry_delta is not None
        assert DELTA_MIN <= pos.entry_delta <= DELTA_MAX

    def test_a_quote_spot_of_zero_does_not_win(self):
        pos = _capture(_pos(), _quote(underlying_price=0.0), _ltp(),
                       spot_price=SPOT)
        assert pos.entry_delta is not None

    def test_no_spot_anywhere_leaves_the_position_untouched(self):
        pos = _capture(_pos(), _quote(underlying_price=None), _ltp(),
                       spot_price=None)
        assert pos.entry_delta is None
        assert pos.last_delta is None
        assert pos.greeks_as_of is None

    def test_uncomputeable_greeks_leave_the_position_untouched(self):
        pos = _capture(_pos(expiry="not-a-date"), _quote(), _ltp())
        assert pos.entry_delta is None
        assert pos.last_delta is None

    def test_expired_contract_is_left_to_the_settlement_path(self):
        pos = _capture(_pos(expiry="2020-01-02"), _quote(), _ltp())
        assert pos.last_delta is None


class TestRobustness:
    def test_position_that_rejects_greeks_attributes_does_not_raise(self):
        """Observability must never be the reason a mark fails."""

        class Rigid:
            strike = STRIKE
            expiry = EXPIRY
            option_type = "CE"

        # Would raise AttributeError on assignment if the guard were missing.
        _capture(Rigid(), _quote(), _ltp())

    def test_no_volatility_in_the_payload_still_recovers_from_the_premium(self):
        """Upstox may omit IV; the premium is enough to invert it, so greeks
        stay available without a second market-data call."""
        pos = _capture(_pos(), _quote(iv=None), _ltp())
        assert pos.entry_delta is not None
        assert pos.entry_iv == pytest.approx(0.15, abs=1e-3)

    def test_put_greeks_have_negative_delta(self):
        pos = _capture(
            _pos(option_type="PE"), _quote(), _ltp(option_type="PE")
        )
        assert pos.entry_delta is not None
        assert pos.entry_delta < 0.0

    def test_zero_premium_does_not_undo_a_known_volatility(self):
        """Greeks come from the quote's volatility, so a zero or stale premium
        cannot corrupt them. (The premium is only needed to *recover* a
        volatility the payload omitted.)"""
        pos = _capture(_pos(), _quote(iv=0.15), 0.0)
        assert DELTA_MIN <= pos.entry_delta <= DELTA_MAX

    def test_zero_premium_with_no_volatility_yields_no_greeks(self):
        pos = _capture(_pos(), _quote(iv=None), 0.0)
        assert pos.entry_delta is None
        assert pos.last_delta is None


class TestVolatilityUnits:
    """Regression guard for the percent/decimal trap.

    Upstox sends IV in percentage points. A sigma of 15.0 where 0.15 belongs
    does not raise -- it silently produces 1500% volatility, so the position
    looks like a deep ITM call with a delta pinned at 1.0 and the decay rule
    sees no movement at all. The conversion lives in the quote parser, so the
    controller's input is already a decimal; this pins that end to end.
    """

    def test_provider_scale_percent_does_not_reach_the_greeks(self):
        """End to end: the parser's decimal feeds the controller unchanged."""
        from trading_system.india.option_quotes import OptionQuote

        parsed = OptionQuote(
            instrument=SimpleNamespace(
                contract_id="NIFTY_CE_1", key="NSE_INDEX:NIFTY25JUN25000CE"
            ),
            ltp=_ltp(),
            timestamp=datetime.now(UTC),
            fetched_at=datetime.now(UTC),
            bid_iv=0.142,   # what _normalise_iv stores for Upstox's "14.2"
            ask_iv=0.158,   # ...and "15.8"
        )
        assert parsed.implied_vol == pytest.approx(0.15)

        pos = _capture(_pos(), parsed, _ltp(), spot_price=SPOT)
        assert DELTA_MIN <= pos.entry_delta <= DELTA_MAX
        assert pos.entry_iv == pytest.approx(0.15)

    def test_a_decimal_sigma_gives_a_bounded_atm_delta(self):
        """An ATM call delta pinned at 1.0 is the signature of the bug."""
        pos = _capture(_pos(), _quote(iv=0.15), _ltp(iv=0.15))
        assert 0.0 < pos.entry_delta < 1.0
        assert pos.entry_delta != pytest.approx(1.0, abs=1e-6)

    def test_the_unconverted_percentage_would_have_been_wrong(self):
        """Documents what the guard is worth: feeding Upstox's raw 15.0 through
        as sigma prices a 1500% volatility, and the delta saturates."""
        unconverted = _capture(_pos(), _quote(iv=15.0), _ltp())
        correct = _capture(_pos(), _quote(iv=0.15), _ltp())
        assert unconverted.entry_delta != pytest.approx(
            correct.entry_delta, abs=0.01
        )
