"""Application configuration and environment variables."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


# Backend directory is the parent of this file's directory
_BACKEND_DIR = Path(__file__).parent.resolve()


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    # Upstox
    upstox_token_encryption_key: str = ""

    # Upstox WebSocket credentials
    # NOTE: Upstox does not have a "service account" concept.
    # The WebSocket requires a valid user's client_id and access_token.
    # For a shared market data feed, you must use a dedicated Upstox account
    # whose sole purpose is to provide market data. This account's credentials
    # are configured here and are separate from end-user broker connections.
    upstox_client_id: str = ""
    upstox_service_account_token: str = ""  # Access token for the dedicated market data account

    # Application
    environment: str = "development"
    log_level: str = "INFO"

    # Live pipeline configuration
    live_pipeline_enabled: bool = False

    # Signal universe - comma-separated list of symbols to analyze
    # These are the symbols for which signals are generated
    signal_universe: str = "NSE:SBIN,NSE:RELIANCE,NSE:TCS,NSE:INFY,NSE:HDFCBANK,NSE:ICICIBANK,NSE:KOTAKBANK,NSE:AXISBANK,NSE:LT,NSE:WIPRO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
