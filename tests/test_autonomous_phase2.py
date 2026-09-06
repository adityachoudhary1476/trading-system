"""Phase 2 - Autonomous Market Scanner tests.

Covers ScannerConfig / MarketUniverse validation, data-provider resolution,
the _evaluate_symbol eligibility pipeline (every rejection path), scan()
result properties (determinism, max_candidates cap, disabled / empty paths),
and AutonomousController.scan_market() integration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest
from pydantic import ValidationError

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.scanner import (
    MarketScanResult,
    MarketScanner,
    MarketUniverse,
    RejectionReason,
    ScannerConfig,
    _resolve_data_provider,
)
from trading_system.data.base import MarketDataProvider
from trading_system.india.market_calendar import SessionPhase, TradingCalendar
from trading_system.paper.control import PaperTradingControlCenter

UTC = timezone.utc

# 2024-01-01 = Monday.
# NSE regular session: 09:15-15:30 IST = 03:45-10:00 UTC (exclusive end).
REGULAR_TS = datetime(2024, 1, 1, 5, 0, tzinfo=UTC)   # 10:30 IST -> REGULAR
CLOSED_TS = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)     # 16:30 IST -> CLOSED
SCAN_TS = datetime(2024, 1, 1, 7, 0, tzinfo=UTC)       # 12:30 IST (scan ref)
SCAN_TS_AFTER_CLOSED = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)  # 17:30 IST


# --------------------------------------------------------------------------- #
# Data fixtures / helpers
# --------------------------------------------------------------------------- #

def _make_ohlcv_df(timestamps, close=100.0, volume=1000.0):
    """Valid OHLCV frame with a tz-aware UTC DatetimeIndex."""
    n = len(timestamps)
    idx = pd.DatetimeIndex(timestamps, tz="UTC")
    return pd.DataFrame(
        {
            "open": [close * 0.99] * n,
            "high": [close * 1.01] * n,
            "low": [close * 0.98] * n,
            "close": [close] * n,
            "volume": [volume] * n,
        },
        index=idx,
    )


def _regular_bars(n=2, end_ts=REGULAR_TS, close=100.0, volume=1000.0):
    stamps = [end_ts - timedelta(hours=i) for i in range(n - 1, -1, -1)]
    return _make_ohlcv_df(stamps, close=close, volume=volume)


def _closed_bars(n=2):
    stamps = [CLOSED_TS - timedelta(hours=i) for i in range(n - 1, -1, -1)]
    return _make_ohlcv_df(stamps)


def _build_scanner(symbols, data=None, scan_ts=SCAN_TS, **overrides):
    """Build a MarketScanner with a callable provider backed by *data*."""
    provider_data = data or {}

    def _provider(symbol, timeframe):
        return provider_data.get(symbol)

    cfg = ScannerConfig(
        universe=MarketUniverse(symbols=list(symbols)),
        timeframe="1h",
        scan_timestamp=scan_ts,
        data_provider=_provider,
        **overrides,
    )
    return MarketScanner(cfg)


def _fresh_df():
    """Bars ending at 05:00 UTC (= 10:30 IST, regular) on a recent weekday."""
    now = datetime.now(UTC)
    end = (now - timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return _make_ohlcv_df(
        [end - timedelta(hours=2), end - timedelta(hours=1), end]
    )


class _MockProvider(MarketDataProvider):
    def __init__(self, data=None):
        self._data = data or {}

    def get_historical(self, symbol, timeframe, limit, start=None, end=None):
        return self._data.get(symbol)

    def get_latest_price(self, symbol):
        return 0.0


def _make_bot_config(allowed_symbols=None, enabled=True):
    if allowed_symbols is None:
        allowed_symbols = frozenset({"NSE:SBIN", "NSE:TCS"})
    return AutonomousBotConfig(
        bot_id="bot-test-001",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=enabled,
        user_constraints=UserConstraints(
            allowed_symbols=allowed_symbols,
            allowed_strategy_ids=frozenset({"strat-001"}),
            allowed_timeframes=frozenset({"1m", "5m", "15m", "1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )


def _make_controller(allowed_symbols=None, enabled=True, data=None):
    config = _make_bot_config(allowed_symbols=allowed_symbols, enabled=enabled)
    cc = MagicMock(spec=PaperTradingControlCenter)
    provider_data = data or {}
    cc.load_market_data = lambda symbol, timeframe: provider_data.get(symbol)
    cc.list_deployments.return_value = []
    return AutonomousController(config=config, control_center=cc)


# --------------------------------------------------------------------------- #
# ScannerConfig validation
# --------------------------------------------------------------------------- #

class TestScannerConfig:
    def test_config_defaults(self):
        cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1h",
            data_provider=lambda s, tf: None,
        )
        assert cfg.enabled is True
        assert cfg.timeframe == "1h"
        assert cfg.max_candidates is None
        assert cfg.max_freshness == timedelta(days=7)
        assert cfg.min_liquidity is None
        assert cfg.require_regular_session is True

    def test_config_rejects_zero_max_freshness(self):
        with pytest.raises(ValidationError):
            ScannerConfig(
                universe=MarketUniverse(symbols=["NSE:SBIN"]),
                timeframe="1h",
                data_provider=lambda s, tf: None,
                max_freshness=timedelta(0),
            )

    def test_config_rejects_negative_max_freshness(self):
        with pytest.raises(ValidationError):
            ScannerConfig(
                universe=MarketUniverse(symbols=["NSE:SBIN"]),
                timeframe="1h",
                data_provider=lambda s, tf: None,
                max_freshness=timedelta(days=-1),
            )

    def test_config_rejects_zero_max_candidates(self):
        with pytest.raises(ValidationError):
            ScannerConfig(
                universe=MarketUniverse(symbols=["NSE:SBIN"]),
                timeframe="1h",
                data_provider=lambda s, tf: None,
                max_candidates=0,
            )

    def test_config_rejects_empty_timeframe(self):
        with pytest.raises(ValidationError):
            ScannerConfig(
                universe=MarketUniverse(symbols=["NSE:SBIN"]),
                timeframe="",
                data_provider=lambda s, tf: None,
            )

    def test_config_resolved_scan_timestamp_applies_utc(self):
        naive = datetime(2024, 1, 1, 7, 0)
        cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1h",
            scan_timestamp=naive,
            data_provider=lambda s, tf: None,
        )
        ts = cfg.resolved_scan_timestamp()
        assert ts.tzinfo is not None
        assert ts == datetime(2024, 1, 1, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# MarketUniverse
# --------------------------------------------------------------------------- #

class TestMarketUniverse:
    def test_universe_normalizes_exchange_only(self):
        uni = MarketUniverse(symbols=["nse:SBIN", "NSE:TCS"])
        assert "NSE:SBIN" in uni.symbols
        assert "NSE:TCS" in uni.symbols

    def test_universe_dedup_preserves_order(self):
        uni = MarketUniverse(symbols=["NSE:SBIN", "NSE:TCS", "nse:SBIN"])
        assert list(uni.symbols) == ["NSE:SBIN", "NSE:TCS"]

    def test_universe_rejects_invalid_symbol(self):
        with pytest.raises(ValidationError):
            MarketUniverse(symbols=["INVALID"])

    def test_universe_empty(self):
        uni = MarketUniverse(symbols=[])
        assert uni.size == 0
        assert len(uni) == 0

    def test_universe_contains(self):
        uni = MarketUniverse(symbols=["NSE:SBIN"])
        assert "NSE:SBIN" in uni
        assert "nse:SBIN" in uni
        assert "NSE:TCS" not in uni

    def test_universe_iteration(self):
        uni = MarketUniverse(symbols=["NSE:SBIN", "NSE:TCS"])
        assert list(uni) == ["NSE:SBIN", "NSE:TCS"]
        assert len(uni) == 2


# --------------------------------------------------------------------------- #
# Data provider resolution
# --------------------------------------------------------------------------- #

class TestDataProviderResolution:
    def test_resolve_none_returns_noop(self):
        provider = _resolve_data_provider(None, 250)
        assert provider("NSE:SBIN", "1h") is None

    def test_resolve_callable_passthrough(self):
        def my_provider(symbol, timeframe):
            return None

        resolved = _resolve_data_provider(my_provider, 250)
        assert resolved is my_provider

    def test_resolve_abc_instance(self):
        df = _regular_bars()
        mp = _MockProvider({"NSE:SBIN": df})
        resolved = _resolve_data_provider(mp, 250)
        assert resolved is not mp
        assert resolved("NSE:SBIN", "1h") is df

    def test_resolve_non_callable_raises(self):
        with pytest.raises(TypeError):
            _resolve_data_provider(42, 250)


# --------------------------------------------------------------------------- #
# Eligibility pipeline (_evaluate_symbol paths)
# --------------------------------------------------------------------------- #

class TestEligibilityPipeline:
    def test_eligible_symbol(self):
        df = _regular_bars()
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": df})
        result = scanner.scan()
        assert result.eligible_count == 1
        c = result.candidates[0]
        assert c.symbol == "NSE:SBIN"
        assert c.latest_price == 100.0
        assert c.data_points == 2
        assert c.freshness_seconds is not None
        assert c.metadata["session_phase"] == "regular"

    def test_invalid_symbol(self):
        scanner = _build_scanner(["NSE:SBIN"], {})
        candidate, rejection = scanner._evaluate_symbol("NO_COLON", SCAN_TS, "scan_id")
        assert candidate is None
        assert rejection.reason == RejectionReason.INVALID_SYMBOL

    def test_missing_data_none(self):
        scanner = _build_scanner(["NSE:SBIN"], {})
        result = scanner.scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.MISSING_MARKET_DATA

    def test_missing_data_empty_df(self):
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": pd.DataFrame()})
        result = scanner.scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.MISSING_MARKET_DATA

    def test_missing_data_exception(self):
        def _raising(symbol, timeframe):
            raise RuntimeError("network exploded")

        cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1h",
            scan_timestamp=SCAN_TS,
            data_provider=_raising,
        )
        result = MarketScanner(cfg).scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.MISSING_MARKET_DATA

    def test_invalid_data_nan(self):
        df = _regular_bars()
        df.loc[df.index[-1], "close"] = float("nan")
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": df})
        result = scanner.scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.INVALID_MARKET_DATA

    def test_invalid_data_non_positive(self):
        df = _regular_bars()
        df.loc[df.index[-1], "close"] = 0.0
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": df})
        result = scanner.scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.INVALID_MARKET_DATA

    def test_future_data_rejected(self):
        df = _regular_bars(end_ts=datetime(2024, 1, 1, 9, 0, tzinfo=UTC))
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": df})
        result = scanner.scan()
        assert result.eligible_count == 0
        r = result.rejections[0]
        assert r.reason == RejectionReason.INVALID_MARKET_DATA
        assert "future" in r.detail.lower()

    def test_stale_data_rejected(self):
        stale_ts = datetime(2023, 12, 25, 5, 0, tzinfo=UTC)
        df = _regular_bars(end_ts=stale_ts)
        scanner = _build_scanner(
            ["NSE:SBIN"], {"NSE:SBIN": df},
            max_freshness=timedelta(days=1),
        )
        result = scanner.scan()
        assert result.eligible_count == 0
        assert result.rejections[0].reason == RejectionReason.STALE_MARKET_DATA

    def test_outside_session_rejected(self):
        df = _closed_bars()
        scanner = _build_scanner(
            ["NSE:SBIN"], {"NSE:SBIN": df},
            scan_ts=SCAN_TS_AFTER_CLOSED,
            require_regular_session=True,
        )
        result = scanner.scan()
        assert result.eligible_count == 0
        r = result.rejections[0]
        assert r.reason == RejectionReason.OUTSIDE_SESSION
        assert r.market_timestamp is not None

    def test_outside_session_allowed_when_not_required(self):
        df = _closed_bars()
        scanner = _build_scanner(
            ["NSE:SBIN"], {"NSE:SBIN": df},
            scan_ts=SCAN_TS_AFTER_CLOSED,
            require_regular_session=False,
        )
        result = scanner.scan()
        assert result.eligible_count == 1
        assert result.candidates[0].metadata["session_phase"] == "closed"

    def test_insufficient_liquidity(self):
        df = _regular_bars(volume=10.0)
        scanner = _build_scanner(
            ["NSE:SBIN"], {"NSE:SBIN": df},
            min_liquidity=1000.0,
        )
        result = scanner.scan()
        assert result.eligible_count == 0
        r = result.rejections[0]
        assert r.reason == RejectionReason.INSUFFICIENT_LIQUIDITY

    def test_no_liquidity_gate_passes(self):
        df = _regular_bars(volume=0.0)
        scanner = _build_scanner(
            ["NSE:SBIN"], {"NSE:SBIN": df},
            min_liquidity=None,
        )
        result = scanner.scan()
        assert result.eligible_count == 1


# --------------------------------------------------------------------------- #
# Scan result properties
# --------------------------------------------------------------------------- #

class TestScanResult:
    def test_scan_id_deterministic(self):
        df = _regular_bars()
        s1 = _build_scanner(["NSE:SBIN", "NSE:TCS"], {"NSE:SBIN": df, "NSE:TCS": df})
        s2 = _build_scanner(["NSE:SBIN", "NSE:TCS"], {"NSE:SBIN": df, "NSE:TCS": df})
        r1 = s1.scan()
        r2 = s2.scan()
        assert r1.scan_id == r2.scan_id
        assert r1.is_deterministic_with(r2)

    def test_candidates_sorted_by_symbol(self):
        df = _regular_bars()
        scanner = _build_scanner(
            ["NSE:TCS", "NSE:SBIN", "NSE:INFY"],
            {"NSE:TCS": df, "NSE:SBIN": df, "NSE:INFY": df},
        )
        result = scanner.scan()
        assert [c.symbol for c in result.candidates] == ["NSE:INFY", "NSE:SBIN", "NSE:TCS"]

    def test_max_candidates_cap(self):
        df = _regular_bars()
        scanner = _build_scanner(
            ["NSE:TCS", "NSE:SBIN", "NSE:INFY"],
            {"NSE:TCS": df, "NSE:SBIN": df, "NSE:INFY": df},
            max_candidates=2,
        )
        result = scanner.scan()
        assert result.eligible_count == 2
        assert len(result.candidates) == 2
        assert [c.symbol for c in result.candidates] == ["NSE:INFY", "NSE:SBIN"]

    def test_disabled_returns_skipped(self):
        df = _regular_bars()
        scanner = _build_scanner(["NSE:SBIN"], {"NSE:SBIN": df}, enabled=False)
        result = scanner.scan()
        assert result.enabled is False
        assert result.scanned_count == 0
        assert result.skipped_count == 1
        assert result.eligible_count == 0
        assert result.rejected_count == 0

    def test_empty_universe(self):
        scanner = _build_scanner([], {})
        result = scanner.scan()
        assert result.universe_size == 0
        assert result.scanned_count == 0
        assert result.eligible_count == 0

    def test_counts_consistent(self):
        df = _regular_bars()
        scanner = _build_scanner(
            ["NSE:SBIN", "NSE:TCS"],
            {"NSE:SBIN": df, "NSE:TCS": None},
        )
        result = scanner.scan()
        assert result.universe_size == 2
        assert result.scanned_count == 2
        assert result.eligible_count == 1
        assert result.rejected_count == 1
        assert result.eligible_count + result.rejected_count == result.scanned_count

    def test_scan_timestamp_defaults_now(self):
        cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1h",
            data_provider=lambda s, tf: None,
        )
        result = MarketScanner(cfg).scan()
        parsed = datetime.fromisoformat(result.scan_timestamp)
        assert parsed.tzinfo is not None

    def test_config_snapshot_present(self):
        scanner = _build_scanner(["NSE:SBIN"], {})
        result = scanner.scan()
        assert "enabled" in result.config_snapshot
        assert result.config_snapshot["timeframe"] == "1h"
        assert result.config_snapshot["universe_size"] == 1


# --------------------------------------------------------------------------- #
# Controller integration
# --------------------------------------------------------------------------- #

class TestControllerIntegration:
    def test_scan_market_returns_result(self):
        controller = _make_controller(data={"NSE:SBIN": _fresh_df()})
        result = controller.scan_market()
        assert isinstance(result, MarketScanResult)
        assert result.universe_size == 2
        assert result.scanned_count == 2
        assert result.eligible_count + result.rejected_count == 2

    def test_scan_market_sets_last_scan_timestamp(self):
        controller = _make_controller(data={"NSE:SBIN": _fresh_df()})
        assert controller.config.last_scan_timestamp is None
        controller.scan_market()
        assert controller.config.last_scan_timestamp is not None
        parsed = datetime.fromisoformat(controller.config.last_scan_timestamp)
        assert parsed.tzinfo is not None

    def test_scan_market_no_provider_all_missing(self):
        controller = _make_controller()
        result = controller.scan_market()
        assert result.eligible_count == 0
        assert result.rejected_count == 2
        for r in result.rejections:
            assert r.reason == RejectionReason.MISSING_MARKET_DATA

    def test_scan_market_does_not_create_deployments(self):
        controller = _make_controller(data={"NSE:SBIN": _fresh_df()})
        before = controller.config.decision_count
        controller.scan_market()
        assert controller.config.decision_count == before

    def test_scan_market_disabled(self):
        controller = _make_controller(enabled=False)
        result = controller.scan_market()
        assert result.enabled is False
        assert result.skipped_count == 2

    def test_scan_market_uses_allowed_symbols(self):
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:INFY", "NSE:HDFCBANK"}),
        )
        result = controller.scan_market()
        assert result.universe_size == 3
        assert result.scanned_count == 3
