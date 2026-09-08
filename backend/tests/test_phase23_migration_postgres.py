"""Regression tests for the Phase 23 forward migration of ``paper_deployments``
(scheduler heartbeat columns).

This test verifies that the v3→v4 migration step in
``EvidenceStore.ensure_schema_current()`` generates correct PostgreSQL DDL
and works correctly for both SQLite and PostgreSQL dialects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


@pytest.fixture
def sqlite_legacy_db(tmp_path):
    """Create a SQLite DB with the legacy paper_deployments schema
    (pre-Phase 23 — missing the 7 heartbeat columns)."""
    db_path = tmp_path / "legacy.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE paper_deployments ("
            "deployment_id VARCHAR(64) PRIMARY KEY,"
            "strategy_id VARCHAR(64) NOT NULL,"
            "strategy_spec_hash VARCHAR(64) NOT NULL,"
            "symbol VARCHAR(32) NOT NULL,"
            "timeframe VARCHAR(8) NOT NULL,"
            "dataset_id VARCHAR(64) NOT NULL,"
            "config_json TEXT NOT NULL DEFAULT '{}',"
            "status VARCHAR(16) NOT NULL DEFAULT 'created',"
            "evidence_ids_json TEXT NOT NULL DEFAULT '[]',"
            "created_at DATETIME NOT NULL,"
            "activated_at DATETIME,"
            "updated_at DATETIME NOT NULL,"
            "notes TEXT NOT NULL DEFAULT '',"
            "options_enabled BOOLEAN NOT NULL DEFAULT FALSE,"
            "allowed_option_types_json TEXT NOT NULL DEFAULT '[\"CE\",\"PE\"]',"
            "max_options_contracts_per_trade INTEGER"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE schema_versions ("
            "id INTEGER PRIMARY KEY,"
            "version INTEGER NOT NULL,"
            "updated_at DATETIME NOT NULL"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO schema_versions (id, version, updated_at) "
            "VALUES (1, 3, :ts)"
        ), {"ts": datetime.now(timezone.utc)})
        conn.execute(text(
            "INSERT INTO paper_deployments ("
            "deployment_id, strategy_id, strategy_spec_hash,"
            "symbol, timeframe, dataset_id, config_json, status,"
            "evidence_ids_json, created_at, activated_at, updated_at, notes,"
            "options_enabled, allowed_option_types_json,"
            "max_options_contracts_per_trade"
            ") VALUES ("
            "'legacy-dep', 'sma5-legacy', 'a' * 64,"
            "'NSE:SBIN', '1d', 'market_data', '{}', 'active',"
            "'[]', :ts, :ts, :ts, '', 0, '[\"CE\",\"PE\"]', 1"
            ")"
        ), {"ts": datetime.now(timezone.utc)})
    yield engine
    engine.dispose()


class TestPhase23MigrationSQLite:
    """Tests for Phase 23 migration on SQLite."""

    def test_ensure_schema_current_adds_heartbeat_columns_sqlite(self, sqlite_legacy_db):
        """The v3→v4 migration step adds the 7 missing heartbeat columns on SQLite."""
        from trading_system.research.evidence import EvidenceStore
        
        version = EvidenceStore(sqlite_legacy_db).ensure_schema_current()
        assert version == 4
        
        inspector = inspect(sqlite_legacy_db)
        cols = {c["name"] for c in inspector.get_columns("paper_deployments")}
        
        # Verify all 7 Phase 23 columns are present
        assert "last_tick_at" in cols
        assert "last_successful_tick_at" in cols
        assert "last_market_data_at" in cols
        assert "last_decision_at" in cols
        assert "last_execution_at" in cols
        assert "worker_id" in cols
        assert "worker_version" in cols

    def test_ensure_schema_current_is_idempotent_sqlite(self, sqlite_legacy_db):
        """A second call must be a safe no-op."""
        from trading_system.research.evidence import EvidenceStore
        
        store = EvidenceStore(sqlite_legacy_db)
        first = store.ensure_schema_current()
        assert first == 4
        
        second = store.ensure_schema_current()
        assert second == 4
        
        inspector = inspect(sqlite_legacy_db)
        cols = {c["name"] for c in inspector.get_columns("paper_deployments")}
        assert "last_tick_at" in cols


class TestPhase23MigrationPostgreSQLDDL:
    """Tests for Phase 23 migration DDL generation for PostgreSQL.
    
    These tests mock a PostgreSQL engine to verify that the correct
    TIMESTAMPTZ type is used in the ALTER TABLE statements.
    """

    def _make_mock_engine(self, dialect_name: str):
        """Create a properly mocked engine with dialect."""
        from sqlalchemy.engine import Engine
        from unittest.mock import MagicMock
        from contextlib import contextmanager
        
        mock_engine = MagicMock(spec=Engine)
        mock_dialect = MagicMock()
        mock_dialect.name = dialect_name
        mock_engine.dialect = mock_dialect
        
        # Create a mock connection that supports context manager protocol
        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=None)
        mock_conn.execute = Mock()
        
        # Mock begin() to return a context manager
        mock_engine.begin = Mock(return_value=mock_conn)
        mock_engine.connect = Mock(return_value=mock_conn)
        
        return mock_engine, mock_conn

    def test_postgres_dialect_uses_timestamptz(self):
        """Verify that PostgreSQL dialect generates TIMESTAMPTZ columns."""
        from trading_system.research.evidence import EvidenceStore
        
        # Create a mock PostgreSQL engine
        mock_engine, mock_conn = self._make_mock_engine("postgresql")
        
        # Mock inspector
        mock_inspector = Mock()
        mock_inspector.has_table.return_value = True
        mock_inspector.get_columns.return_value = [
            {"name": "deployment_id"},
            {"name": "strategy_id"},
            # ... existing columns, but NOT the Phase 23 columns
        ]
        
        # Capture the DDL executed
        executed_ddl = []
        mock_conn.execute = Mock(side_effect=lambda ddl: executed_ddl.append(str(ddl)))
        
        with patch("sqlalchemy.inspect", return_value=mock_inspector):
            with patch("trading_system.research.evidence.Base") as mock_base:
                mock_base.metadata.create_all = Mock()
                
                # Mock schema version methods
                store = EvidenceStore.__new__(EvidenceStore)
                store.engine = mock_engine
                store._schema_version = Mock(return_value=3)
                store._set_schema_version = Mock()
                
                # Call the migration
                result = store.ensure_schema_current()
                
                # Verify TIMESTAMPTZ was used in DDL
                assert result == 4
                
                # Check that all timestamp columns use TIMESTAMPTZ
                timestamp_cols = [
                    "last_tick_at",
                    "last_successful_tick_at",
                    "last_market_data_at",
                    "last_decision_at",
                    "last_execution_at",
                ]
                
                for col in timestamp_cols:
                    # Find the DDL for this column
                    col_ddl = next((ddl for ddl in executed_ddl if col in ddl), None)
                    assert col_ddl is not None, f"No DDL found for column {col}"
                    assert "TIMESTAMPTZ" in col_ddl.upper(), \
                        f"Expected TIMESTAMPTZ in DDL for {col}, got: {col_ddl}"
                    assert "DATETIME" not in col_ddl.upper(), \
                        f"DATETIME should not appear in PostgreSQL DDL for {col}: {col_ddl}"
                
                # Verify VARCHAR columns are unchanged
                varchar_cols = ["worker_id", "worker_version"]
                for col in varchar_cols:
                    col_ddl = next((ddl for ddl in executed_ddl if col in ddl), None)
                    assert col_ddl is not None, f"No DDL found for column {col}"
                    assert "VARCHAR" in col_ddl.upper(), \
                        f"Expected VARCHAR in DDL for {col}, got: {col_ddl}"

    def test_sqlite_dialect_uses_datetime(self):
        """Verify that SQLite dialect still generates DATETIME columns."""
        from trading_system.research.evidence import EvidenceStore
        
        # Create a mock SQLite engine
        mock_engine, mock_conn = self._make_mock_engine("sqlite")
        
        mock_inspector = Mock()
        mock_inspector.has_table.return_value = True
        mock_inspector.get_columns.return_value = [
            {"name": "deployment_id"},
            {"name": "strategy_id"},
        ]
        
        # Capture the DDL executed
        executed_ddl = []
        mock_conn.execute = Mock(side_effect=lambda ddl: executed_ddl.append(str(ddl)))
        
        with patch("sqlalchemy.inspect", return_value=mock_inspector):
            with patch("trading_system.research.evidence.Base") as mock_base:
                mock_base.metadata.create_all = Mock()
                
                store = EvidenceStore.__new__(EvidenceStore)
                store.engine = mock_engine
                store._schema_version = Mock(return_value=3)
                store._set_schema_version = Mock()
                
                result = store.ensure_schema_current()
                
                assert result == 4
                
                # Check that all timestamp columns use DATETIME for SQLite
                timestamp_cols = [
                    "last_tick_at",
                    "last_successful_tick_at",
                    "last_market_data_at",
                    "last_decision_at",
                    "last_execution_at",
                ]
                
                for col in timestamp_cols:
                    col_ddl = next((ddl for ddl in executed_ddl if col in ddl), None)
                    assert col_ddl is not None, f"No DDL found for column {col}"
                    assert "DATETIME" in col_ddl.upper(), \
                        f"Expected DATETIME in SQLite DDL for {col}, got: {col_ddl}"
                    assert "TIMESTAMPTZ" not in col_ddl.upper(), \
                        f"TIMESTAMPTZ should not appear in SQLite DDL for {col}: {col_ddl}"


class TestPhase23MigrationIntegration:
    """Integration tests that verify the migration works with real SQLite."""

    def test_legacy_rows_remain_readable_post_migration(self, sqlite_legacy_db):
        """A row written before the migration must round-trip with heartbeat fields as NULL."""
        from trading_system.research.evidence import EvidenceStore
        from trading_system.paper.control import PaperTradingControlCenter
        
        EvidenceStore(sqlite_legacy_db).ensure_schema_current()
        
        center = PaperTradingControlCenter.from_engine(sqlite_legacy_db)
        deps = center.list_deployments()
        assert len(deps) == 1
        d = deps[0]
        assert d.deployment_id == "legacy-dep"
        # Heartbeat fields should be None for legacy rows
        assert d.last_tick_at is None
        assert d.last_successful_tick_at is None
        assert d.last_market_data_at is None
        assert d.last_decision_at is None
        assert d.last_execution_at is None
        assert d.worker_id is None
        assert d.worker_version is None

    def test_new_deployment_persists_heartbeat_fields(self, sqlite_legacy_db):
        """A PaperDeployment written after the migration must persist heartbeat fields."""
        from trading_system.research.evidence import EvidenceStore
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.paper.deployment import (
            PaperDeployment,
            PaperDeploymentConfig,
            PaperDeploymentStatus,
        )
        from datetime import datetime, timezone
        
        EvidenceStore(sqlite_legacy_db).ensure_schema_current()
        center = PaperTradingControlCenter.from_engine(sqlite_legacy_db)
        
        # Create a new deployment with heartbeat fields
        now = datetime.now(timezone.utc).isoformat()
        deployment = PaperDeployment(
            deployment_id="new-dep",
            strategy_id="new-strat",
            strategy_spec_hash="c" * 64,
            symbol="NSE:TCS",
            timeframe="1d",
            dataset_id="market_data",
            config=PaperDeploymentConfig(),
            status=PaperDeploymentStatus.ACTIVE,
            created_at=now,
            activated_at=now,
            updated_at=now,
            notes="",
            last_tick_at=now,
            last_successful_tick_at=now,
            last_market_data_at=now,
            last_decision_at=now,
            last_execution_at=now,
            worker_id="worker-123",
            worker_version="1.0.0",
        )
        
        with center.registry.store._Session() as s:
            s.add(deployment.as_record())
            s.commit()
        
        # Re-read and verify
        deps = center.list_deployments()
        new = next(d for d in deps if d.deployment_id == "new-dep")
        assert new.last_tick_at is not None
        assert new.last_successful_tick_at is not None
        assert new.last_market_data_at is not None
        assert new.last_decision_at is not None
        assert new.last_execution_at is not None
        assert new.worker_id == "worker-123"
        assert new.worker_version == "1.0.0"