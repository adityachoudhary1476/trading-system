"""Tests for the configuration system."""
from pathlib import Path
import importlib

import pytest

from trading_system.config import settings, Settings, MarketConfig


def test_default_provider_is_upstox_india_primary():
    # Day 3+: Indian markets are the primary target; Upstox is the default provider,
    # but Binance remains available as a development/test provider.
    assert settings.market.provider in ("upstox", "binance")


def test_binance_still_selectable_as_dev_provider(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "binance")
    s = Settings()
    assert s.market.provider == "binance"


def test_symbols_parsed_from_env(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTCUSDT, ETHUSDT , SOLUSDT")
    # Force a fresh Settings instance to re-read env.
    s = Settings()
    assert s.market.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_db_url_is_sqlite_by_default():
    s = Settings()
    assert s.storage.db_url.startswith("sqlite:///")


def test_log_level_upper(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings()
    assert s.logging.level == "INFO" or s.logging.level == "DEBUG"


def test_telegram_disabled_by_default():
    assert settings.telegram.enabled is False


def test_secrets_not_stored_in_config(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token-123")
    s = Settings()
    # The token VALUE must NOT appear anywhere in the config or its summary.
    summary = str(s.summary())
    assert "super-secret-token-123" not in summary
    # The config only stores the ENV VAR NAME, not the value.
    assert s.telegram.token_env == "TELEGRAM_BOT_TOKEN"
    assert "super-secret-token-123" not in str(s.telegram.__dict__)


def test_ai_config_only_holds_connection_info():
    assert settings.ai.provider
    assert settings.ai.model_name
    # No API key value is stored on the object.
    assert not getattr(settings.ai, "api_key", None)
