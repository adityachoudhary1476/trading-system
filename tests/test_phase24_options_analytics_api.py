"""Per-strike greeks analytics endpoint.

Read-only by construction: the endpoint reports what the chain looks like and
must never be able to place an order or enable trading. The tests below pin that
alongside the payload shape, because an analytics surface that quietly became
order-capable would be a much worse regression than a wrong number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from trading_system.autonomous.greeks import black_scholes_price, years_to_expiry
from trading_system.paper_api import PaperAPIRouter

UTC = timezone.utc
NOW = datetime.now(UTC).replace(hour=9, minute=30, second=0, microsecond=0)
EXPIRY = (NOW.date() + timedelta(days=45)).isoformat()
SPOT = 25000.0
YEARS = years_to_expiry(EXPIRY, now=NOW)


def _quote(strike, option_type, *, iv=0.15, oi=None, change_oi=None):
    ltp = black_scholes_price(
        spot=SPOT, strike=strike, years_to_expiry=YEARS,
        rate=0.065, sigma=iv, option_type=option_type,
    )
    return SimpleNamespace(
        strike=float(strike),
        bid=round(ltp - 0.5, 2), ask=round(ltp + 0.5, 2),
        last=ltp, ltp=ltp,
        open_interest=oi, change_oi=change_oi,
        instrument_key=f"NSE_INDEX:NIFTY{option_type}{strike:g}",
        option_type=option_type,
        # Provider-native percent.
        bid_iv=iv * 100.0, ask_iv=iv * 100.0,
    )


def _chain(strikes=(24500.0, 25000.0, 25500.0), *, spot=SPOT, expiry=EXPIRY,
           chains=None):
    if chains is not None:
        return chains
    return SimpleNamespace(
        underlying="NIFTY", expiry=expiry, spot_price=spot,
        strike_interval=50.0, strikes=[float(s) for s in strikes],
        call_quotes={float(s): _quote(s, "CE") for s in strikes},
        put_quotes={float(s): _quote(s, "PE") for s in strikes},
    )


class _StubProvider:
    """Minimal chain provider: returns a fixed chain and counts its calls."""

    def __init__(self, chain, expiries=None):
        self._chain = chain
        self.expiries = [] if expiries is None else list(expiries)
        self.calls: list[tuple[str, str]] = []
        self.expiry_calls: list[str] = []

    def get_chain(self, underlying, expiry):
        self.calls.append((underlying, expiry))
        return self._chain

    def list_expiries(self, underlying):
        self.expiry_calls.append(underlying)
        return list(self.expiries)

    def is_healthy(self):
        return True

    def persistence_status(self):
        return {"healthy": True, "failures": 0, "last_error": None,
                "last_error_at": None}


def _router(engine, chain=None, expiries=None):
    """A router whose controller carries a stub chain provider.

    Builds on the same controller stand-in the options-capability tests use, so
    the route resolves exactly as it does in production rather than through a
    hand-assembled context that could drift from the real one.
    """
    from trading_system.paper.control import PaperTradingControlCenter
    from tests.test_phase8_options_capability import _build_eligible_deployment

    store, registry, intelligence, gate, dep = _build_eligible_deployment(engine)
    controller = SimpleNamespace(
        _chain_provider=_StubProvider(
            _chain() if chain is None else chain, expiries=expiries,
        ),
    )
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence, gate=gate,
    )
    return PaperAPIRouter(center, controller=controller), dep


@pytest.fixture
def engine():
    return create_engine("sqlite://")


def _get(router, dep, query=""):
    return router.dispatch(
        "GET",
        f"/deployments/{dep.deployment_id}/options/analytics/NIFTY{query}",
    )


class TestPayload:
    def test_returns_greeks_for_every_strike_and_side(self, engine):
        router, dep = _router(engine)
        env = _get(router, dep, f"?expiry={EXPIRY}")
        assert env.status == 200
        body = env.body
        assert body["underlying"] == "NIFTY"
        assert body["expiry"] == EXPIRY
        assert body["spot_price"] == pytest.approx(SPOT)
        assert body["strike_interval"] == pytest.approx(50.0)
        # 3 strikes x 2 sides
        assert len(body["rows"]) == 6
        assert {r["option_type"] for r in body["rows"]} == {"CE", "PE"}
        assert all(r["delta"] is not None for r in body["rows"])

    def test_provider_percent_iv_is_reported_as_a_decimal(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        row = next(
            r for r in body["rows"]
            if r["strike"] == 25000.0 and r["option_type"] == "CE"
        )
        assert row["implied_vol"] == pytest.approx(0.15)
        assert row["iv_source"] == "quote"
        assert 0.45 <= row["delta"] <= 0.60

    def test_summary_reports_atm_and_aggregates(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        summary = body["summary"]
        assert summary["atm_strike"] == pytest.approx(25000.0)
        assert summary["rows_total"] == 6
        assert summary["rows_with_greeks"] == 6
        assert summary["coverage"] == pytest.approx(1.0)
        assert summary["call_delta_total"] > 0
        assert summary["put_delta_total"] < 0
        assert summary["net_delta"] is not None
        assert summary["vega_total"] > 0

    def test_moneyness_is_labelled(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        rows = {(r["strike"], r["option_type"]): r for r in body["rows"]}
        assert rows[(24500.0, "CE")]["moneyness"] == "ITM"
        assert rows[(25500.0, "CE")]["moneyness"] == "OTM"
        assert rows[(25000.0, "CE")]["moneyness"] == "ATM"
        assert rows[(24500.0, "PE")]["moneyness"] == "OTM"
        assert rows[(25500.0, "PE")]["moneyness"] == "ITM"

    def test_oi_and_change_oi_are_passed_through(self, engine):
        strikes = (25000.0,)
        chain = _chain(
            strikes,
            chains=SimpleNamespace(
                underlying="NIFTY", expiry=EXPIRY, spot_price=SPOT,
                strike_interval=50.0, strikes=[25000.0],
                call_quotes={25000.0: _quote(25000.0, "CE", oi=1000,
                                             change_oi=-250)},
                put_quotes={25000.0: _quote(25000.0, "PE", oi=2000)},
            ),
        )
        router, dep = _router(engine, chain)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        call = next(r for r in body["rows"] if r["option_type"] == "CE")
        assert call["oi"] == 1000
        assert call["change_oi"] == -250
        assert body["summary"]["put_call_oi_ratio"] == pytest.approx(2.0)

    def test_unknown_greeks_are_null_not_zero(self, engine):
        """The whole point of Optional: an uncomputable strike must not render
        as a 0.00 delta."""
        chain = _chain(
            chains=SimpleNamespace(
                underlying="NIFTY", expiry=EXPIRY, spot_price=SPOT,
                strike_interval=50.0, strikes=[25000.0],
                call_quotes={
                    25000.0: SimpleNamespace(
                        strike=25000.0, bid=0.0, ask=0.0, last=None, ltp=None,
                        open_interest=None, change_oi=None, instrument_key=None,
                        option_type="CE", bid_iv=None, ask_iv=None,
                    )
                },
                put_quotes={},
            )
        )
        router, dep = _router(engine, chain)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        row = body["rows"][0]
        assert row["delta"] is None
        assert row["implied_vol"] is None
        assert row["iv_source"] is None
        assert body["summary"]["coverage"] == pytest.approx(0.0)
        assert body["summary"]["call_delta_total"] is None


class TestWindowParameter:
    def test_window_narrows_the_row_count(self, engine):
        router, dep = _router(engine)
        wide = _get(router, dep, f"?expiry={EXPIRY}&strikes=5").body
        narrow = _get(router, dep, f"?expiry={EXPIRY}&strikes=0").body
        assert len(narrow["rows"]) < len(wide["rows"])
        assert narrow["summary"]["atm_strike"] == pytest.approx(25000.0)

    def test_window_zero_is_atm_only(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}&strikes=0").body
        assert {r["strike"] for r in body["rows"]} == {25000.0}
        assert body["window_strikes"] == 0

    def test_absent_window_reports_null_so_the_default_is_implicit(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}").body
        assert body["window_strikes"] is None

    def test_huge_window_is_clamped_and_says_so(self, engine):
        router, dep = _router(engine)
        body = _get(router, dep, f"?expiry={EXPIRY}&strikes=99999").body
        assert body["window_truncated"] is True
        # The requested value is echoed back so a caller can see it was clamped,
        # and the note says what it actually became.
        assert body["window_strikes"] == 99999
        assert any(
            "clamped to the maximum" in n for n in body["notes"]
        ), body["notes"]

    def test_non_integer_window_is_a_400(self, engine):
        router, dep = _router(engine)
        env = _get(router, dep, f"?expiry={EXPIRY}&strikes=wide")
        assert env.status == 400

    def test_negative_window_is_a_400(self, engine):
        """Rejected at the edge rather than silently clamped: a caller asking for
        -1 is a bug, and quietly returning the ATM row hides it."""
        router, dep = _router(engine)
        env = _get(router, dep, f"?expiry={EXPIRY}&strikes=-3")
        assert env.status == 400


class TestFailureModes:
    def test_missing_expiry_is_a_400(self, engine):
        router, dep = _router(engine)
        env = _get(router, dep)
        assert env.status == 400

    def test_no_chain_for_the_expiry_is_a_404(self, engine):
        router, dep = _router(engine, chain=None)
        router._controller._chain_provider = _StubProvider(None)
        env = _get(router, dep, f"?expiry={EXPIRY}")
        assert env.status == 404

    def test_absent_chain_provider_is_a_404(self, engine):
        router, dep = _router(engine)
        router._controller._chain_provider = None
        env = _get(router, dep, f"?expiry={EXPIRY}")
        assert env.status == 404

    def test_unavailable_spot_returns_a_reason_not_a_zero_table(self, engine):
        router, dep = _router(engine, _chain(spot=0.0))
        env = _get(router, dep, f"?expiry={EXPIRY}")
        assert env.status == 200
        assert env.body["rows"] == []
        assert any("spot price is unavailable" in n for n in env.body["notes"])

    def test_expired_chain_returns_null_greeks_with_a_note(self, engine):
        past = (NOW.date() - timedelta(days=1)).isoformat()
        router, dep = _router(engine, _chain(expiry=past))
        env = _get(router, dep, f"?expiry={past}")
        assert env.status == 200
        assert all(r["delta"] is None for r in env.body["rows"])
        assert any("not in the future" in n for n in env.body["notes"])

    def test_nse_prefixed_symbol_is_accepted(self, engine):
        router, dep = _router(engine)
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/options/analytics/NSE:NIFTY"
            f"?expiry={EXPIRY}",
        )
        assert env.status == 200
        assert router._controller._chain_provider.calls[-1] == ("NIFTY", EXPIRY)

    def test_symbol_is_upper_cased(self, engine):
        router, dep = _router(engine)
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/options/analytics/nifty"
            f"?expiry={EXPIRY}",
        )
        assert env.status == 200
        assert router._controller._chain_provider.calls[-1][0] == "NIFTY"


class TestObservationalOnly:
    def test_no_order_path_is_reachable_from_the_route_table(self, engine):
        """The route is GET-only. Nothing here can turn into a submission."""
        router, dep = _router(engine)
        env = router.dispatch(
            "POST",
            f"/deployments/{dep.deployment_id}/options/analytics/NIFTY"
            f"?expiry={EXPIRY}",
        )
        assert env.status in (404, 405)

    def test_fetching_analytics_does_not_change_deployment_state(self, engine):
        router, dep = _router(engine)
        before = dep.as_record().__dict__.copy()
        _get(router, dep, f"?expiry={EXPIRY}")
        after = dep.as_record().__dict__
        for key in ("status", "options_enabled", "config"):
            assert before.get(key) == after.get(key)

    def test_deployment_identity_is_unchanged_by_the_feature(self, engine):
        """The analytics surface must not require a config field, because
        deployment_identity hashes the whole config."""
        router, dep = _router(engine)
        _get(router, dep, f"?expiry={EXPIRY}")
        dumped = dep.config.model_dump(mode="json")
        for forbidden in ("greeks", "delta_decay", "analytics", "window_strikes"):
            assert forbidden not in dumped

    def test_chain_fetch_happens_once_per_request(self, engine):
        router, dep = _router(engine)
        _get(router, dep, f"?expiry={EXPIRY}")
        provider = router._controller._chain_provider
        assert len(provider.calls) == 1

    @pytest.mark.parametrize("path", ["options/analytics", "options/chain"])
    def test_expiry_reaches_the_provider_as_a_string_not_a_list(self, engine, path):
        """Query values arrive as lists. Passing one straight to the provider
        makes an expiry comparison like `expiry = ['2026-11-19']`, which never
        matches a real row, so the endpoint 404s on valid data."""
        router, dep = _router(engine)
        router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/{path}/NIFTY?expiry={EXPIRY}",
        )
        underlying, expiry = router._controller._chain_provider.calls[-1]
        assert isinstance(expiry, str)
        assert expiry == EXPIRY
        assert isinstance(underlying, str)

    @pytest.mark.parametrize(
        "encoded,expected",
        [("NSE%3ANIFTY", "NIFTY"), ("NSE:NIFTY", "NIFTY"), ("NIFTY", "NIFTY"),
         ("nse%3Asbin", "SBIN")],
    )
    def test_percent_encoded_symbols_are_decoded_before_matching(
        self, engine, encoded, expected
    ):
        """The client encodes the symbol, and this codebase writes symbols as
        "NSE:SBIN". Matching a raw "%3A" against a pattern that allows ":" fails,
        which would surface as a confusing route-not-found rather than a result.
        """
        router, dep = _router(engine)
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/options/analytics/{encoded}"
            f"?expiry={EXPIRY}",
        )
        # 200 proves the route matched and the handler ran; a decode failure
        # would be a 404 with the provider never consulted.
        assert env.status == 200
        underlying, _ = router._controller._chain_provider.calls[-1]
        assert underlying == expected

    def test_an_encoded_slash_cannot_invent_a_path_segment(self, engine):
        """Decoding is per-segment so %2F stays inside one segment and cannot
        change which route matches."""
        router, dep = _router(engine)
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/options/analytics/a%2Fb"
            f"?expiry={EXPIRY}",
        )
        assert env.status == 404
        # Handler never ran, so the provider was never consulted.
        assert router._controller._chain_provider.calls == []

# --------------------------------------------------------------------------- #
# Expiry discovery
# --------------------------------------------------------------------------- #
def _future(days):
    # UTC, matching the clock the route uses. Using the local date here would
    # make the past/future boundary disagree by a day depending on the runner's
    # timezone.
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _past(days):
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


def _expiries(router, dep, query=""):
    return router.dispatch(
        "GET", f"/deployments/{dep.deployment_id}/options/expiries{query}"
    )


class TestExpiryListing:
    def test_returns_listed_expiries_in_ascending_order(self, engine):
        router, dep = _router(engine, expiries=[_future(45), _future(7)])
        env = _expiries(router, dep)
        assert env.status == 200
        body = env.body
        assert body["underlying"] == "NIFTY"
        assert body["count"] == 2
        assert body["source"] == "provider"
        assert body["expiries"] == sorted(body["expiries"])
        assert _future(7) in body["expiries"]

    def test_defaults_to_nifty_when_no_symbol_given(self, engine):
        router, dep = _router(engine, expiries=[_future(10)])
        assert _expiries(router, dep).status == 200
        assert router._controller._chain_provider.expiry_calls == ["NIFTY"]

    def test_symbol_is_normalised_before_lookup(self, engine):
        router, dep = _router(engine, expiries=[_future(10)])
        for symbol in ("NSE:NIFTY", "nifty", "NSE%3ASBIN"):
            router._controller._chain_provider.expiry_calls.clear()
            env = _expiries(router, dep, f"?symbol={symbol}")
            assert env.status == 200, symbol
            underlying = env.body["underlying"]
            assert router._controller._chain_provider.expiry_calls == [underlying]
            assert ":" not in underlying
            assert underlying == underlying.upper()

    def test_expired_expiries_are_dropped_and_reported(self, engine):
        router, dep = _router(engine, expiries=[_past(3), _future(20)])
        body = _expiries(router, dep).body
        assert body["expiries"] == [_future(20)]
        assert body["count"] == 1
        # Nothing disappears without saying so.
        assert any("expired" in n for n in body["notes"])

    def test_todays_expiry_is_kept_but_yesterdays_is_not(self, engine):
        """The cut is inclusive of today: an expiry happening today still has a
        live chain, while yesterday's is gone."""
        today = datetime.now(timezone.utc).date().isoformat()
        router, dep = _router(engine, expiries=[_past(1), today])
        body = _expiries(router, dep).body
        assert body["expiries"] == [today]
        assert body["count"] == 1

    def test_unparseable_values_are_dropped_and_reported(self, engine):
        router, dep = _router(engine, expiries=["not-a-date", _future(30)])
        body = _expiries(router, dep).body
        assert body["expiries"] == [_future(30)]
        assert any("unparseable" in n for n in body["notes"])

    def test_empty_list_says_unavailable_rather_than_none_exist(self, engine):
        """The distinction matters: "we could not ask" is not "the exchange lists
        no expiries", and collapsing them would hide an auth failure."""
        router, dep = _router(engine, expiries=[])
        body = _expiries(router, dep).body
        assert body["expiries"] == []
        assert body["count"] == 0
        assert body["source"] == "unavailable"
        assert any("could be listed" in n for n in body["notes"])

    def test_all_expired_reports_unavailable(self, engine):
        router, dep = _router(engine, expiries=[_past(1), _past(30)])
        body = _expiries(router, dep).body
        assert body["expiries"] == []
        assert body["source"] == "unavailable"
        assert any("2 expired expiries omitted" in n for n in body["notes"])

    def test_single_expired_expires_wording_is_singular(self, engine):
        router, dep = _router(engine, expiries=[_past(1)])
        body = _expiries(router, dep).body
        assert any("1 expired expiry omitted" in n for n in body["notes"])

    def test_duplicate_expiries_are_collapsed(self, engine):
        router, dep = _router(engine, expiries=[_future(15), _future(15)])
        body = _expiries(router, dep).body
        assert body["count"] == 1

    def test_absent_chain_provider_is_a_404(self, engine):
        router, dep = _router(engine, expiries=[_future(10)])
        router._controller._chain_provider = None
        assert _expiries(router, dep).status == 404

    def test_provider_without_listing_support_is_a_404(self, engine):
        class NoListing:
            def get_chain(self, u, e):
                return None

        router, dep = _router(engine)
        router._controller._chain_provider = NoListing()
        assert _expiries(router, dep).status == 404

    def test_listing_fetches_no_chain(self, engine):
        """Listing is cheap on purpose: it must not pull a full chain."""
        router, dep = _router(engine, expiries=[_future(10)])
        _expiries(router, dep)
        provider = router._controller._chain_provider
        assert provider.calls == []
        assert provider.expiry_calls == ["NIFTY"]

    def test_listing_is_get_only(self, engine):
        router, dep = _router(engine, expiries=[_future(10)])
        env = router.dispatch(
            "POST", f"/deployments/{dep.deployment_id}/options/expiries"
        )
        assert env.status in (404, 405)

    def test_listed_expiry_can_be_fed_straight_to_analytics(self, engine):
        """The point of the endpoint: a listed value must satisfy the analytics
        route, so the dropdown cannot offer something that then 404s."""
        router, dep = _router(engine, expiries=[_future(30)])
        expiry = _expiries(router, dep).body["expiries"][0]
        env = _get(router, dep, f"?expiry={expiry}")
        assert env.status == 200
        # The listed expiry reaches the provider verbatim; the response reports
        # the expiry of the chain it actually analysed.
        assert router._controller._chain_provider.calls[-1][1] == expiry
