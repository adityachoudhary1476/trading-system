"""Tests for MARKET_DATA_DB_URL resolution in StorageConfig.db_url."""
import os
import pytest

from trading_system.config.settings import Settings, StorageConfig


def test_db_url_uses_postgresql_when_env_set(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_DB_URL", "postgresql://user:pass@host:5432/db")
    s = Settings()
    assert s.storage.db_url == "postgresql://user:pass@host:5432/db"


def test_db_url_falls_back_to_sqlite_when_env_absent(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_DB_URL", raising=False)
    s = Settings()
    assert s.storage.db_url.startswith("sqlite:///")


def test_db_url_falls_back_to_sqlite_when_env_empty(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_DB_URL", "")
    s = Settings()
    assert s.storage.db_url.startswith("sqlite:///")


def test_db_url_postgresql_takes_precedence_over_sqlite(monkeypatch):
    """Verify the PostgreSQL URL takes precedence over the SQLite path."""
    monkeypatch.setenv("MARKET_DATA_DB_URL", "postgresql://pg:secret@db.host:5432/prod")
    s = Settings()
    url = s.storage.db_url
    assert url == "postgresql://pg:secret@db.host:5432/prod"
    assert not url.startswith("sqlite")
