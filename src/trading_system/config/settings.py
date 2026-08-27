"""Centralized, environment-driven configuration.

All secrets are loaded from environment variables / a .env file and are
NEVER hard-coded. This module is intentionally free of any trading, IO,
or provider logic so it can be unit-tested in isolation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env from the project root if present. Safe to call repeatedly.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_PROJECT_ROOT / ".env")


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# --- Timeframe handling -------------------------------------------------------
# Stooq/Binance both use the suffixes below; we keep a canonical enum and
# normalize on the provider boundary.
Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]


@dataclass
class MarketConfig:
    # Target market: "crypto" (Binance dev/test) or "india" (FYERS primary).
    market: str = field(default_factory=lambda: os.getenv("MARKET", "india"))
    provider: str = field(default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "fyers"))
    default_exchange: str = field(default_factory=lambda: os.getenv("DEFAULT_EXCHANGE", "NSE"))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Kolkata"))
    symbols: list[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()
        ]
    )
    timeframe: Timeframe = field(
        default_factory=lambda: os.getenv("TIMEFRAME", "1d")  # type: ignore[assignment]
    )
    # Number of most-recent bars to pull on a default ingestion.
    lookback_bars: int = field(default_factory=lambda: _get_int("LOOKBACK_BARS", 365))
    # AI analysis cadence: build a snapshot every N closed bars (NOT per tick).
    analysis_interval_bars: int = field(default_factory=lambda: _get_int("ANALYSIS_INTERVAL_BARS", 1))
    # Feed is considered STALE if no event arrives within this many seconds.
    stale_seconds: int = field(default_factory=lambda: _get_int("STALE_SECONDS", 60))


@dataclass
class StorageConfig:
    # SQLite by default for Day 1. A relative path is resolved against the
    # project root so the DB travels with the repo's `data/` directory.
    db_path: Path = field(
        default_factory=lambda: (
            _PROJECT_ROOT / os.getenv("DB_PATH", "data/market_data.db")
        )
    )

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@dataclass
class LoggingConfig:
    level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    log_dir: Path = field(
        default_factory=lambda: _PROJECT_ROOT / os.getenv("LOG_DIR", "logs")
    )


@dataclass
class AIConfig:
    """Connection details for a FUTURE AI analyst component.

    This only stores *how to reach* a model. It is wired into the config so
    the AI analyst can be added later without touching data/storage code.
    No AI trading logic exists on Day 1.
    """

    provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", "ollama"))
    model_name: str = field(default_factory=lambda: os.getenv("AI_MODEL", "llama3.1"))
    # OpenAI-style providers (optional, only if used later)
    api_base: str = field(default_factory=lambda: os.getenv("AI_API_BASE", "http://localhost:11434"))
    # Secrets are referenced by ENVIRONMENT VARIABLE NAME, never stored as values.
    api_key_env: str = field(default_factory=lambda: os.getenv("AI_API_KEY_ENV", ""))


@dataclass
class TelegramConfig:
    enabled: bool = field(default_factory=lambda: _get_bool("TELEGRAM_ENABLED", False))
    # Token / chat id are read at send-time from env, never stored here.
    token_env: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN_ENV", "TELEGRAM_BOT_TOKEN"))
    chat_id_env: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID_ENV", "TELEGRAM_CHAT_ID"))


@dataclass
class PaperTradingConfig:
    enabled: bool = field(default_factory=lambda: _get_bool("PAPER_TRADING_ENABLED", False))
    initial_capital: float = field(default_factory=lambda: float(os.getenv("PAPER_CAPITAL", "100000.0")))
    base_currency: str = field(default_factory=lambda: os.getenv("PAPER_CURRENCY", "USDT"))


@dataclass
class Settings:
    market: MarketConfig = field(default_factory=MarketConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    paper_trading: PaperTradingConfig = field(default_factory=PaperTradingConfig)
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)

    def summary(self) -> dict:
        """A redaction-safe view for logging (no secrets)."""
        return {
            "market": self.market.market,
            "data_provider": self.market.provider,
            "default_exchange": self.market.default_exchange,
            "timezone": self.market.timezone,
            "symbols": self.market.symbols,
            "timeframe": self.market.timeframe,
            "lookback_bars": self.market.lookback_bars,
            "analysis_interval_bars": self.market.analysis_interval_bars,
            "db_url": self.storage.db_url,
            "log_level": self.logging.level,
            "ai_provider": self.ai.provider,
            "ai_model": self.ai.model_name,
            "telegram_enabled": self.telegram.enabled,
            "paper_trading_enabled": self.paper_trading.enabled,
        }


# A single shared settings instance for the application.
def load_settings() -> Settings:
    return Settings()


settings = load_settings()
