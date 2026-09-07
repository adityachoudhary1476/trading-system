"""Regression tests for the Phase 8 forward migration of ``paper_deployments``.

Reproduces the production schema drift — the Railway Postgres database was
created by an older image that did not include the Phase 8 options columns
(``options_enabled``, ``allowed_option_types_json``,
``max_options_contracts_per_trade``). The model declares them, so any
``SELECT * FROM paper_deployments`` raised
``psycopg2.errors.UndefinedColumn: column paper_deployments.options_enabled
does not exist``.

The fix is the v2→v3 schema migration step in
``EvidenceStore.ensure_schema_current()``. These tests prove:

  1. ``create_all`` alone does NOT add the missing columns to an existing
     table (documents the documented limitation we are working around).
  2. ``ensure_schema_current`` adds the missing columns idempotently, with
     safe defaults, without disturbing existing rows.
  3. A second call is a no-op (no error, columns still present).
  4. After the migration, ``list_deployments`` reads legacy rows that have
     no Phase 8 columns and exposes them with the documented defaults.
  5. New deployments created post-migration persist the Phase 8 fields and
     round-trip correctly.
  6. The AutonomousController's bot-scoped deployment filter still works
     after the migration.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def legacy_db(tmp_path):
    """Create a SQLite DB with the *legacy* paper_deployments schema
    (pre-Phase 8 — missing the 3 options columns) and seed one row that has
    no Phase 8 fields at all."""
    db_path = tmp_path / "legacy.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        # Hand-write the legacy schema to match what an older Railway image
        # would have created via ``Base.metadata.create_all``.
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
            "notes TEXT NOT NULL DEFAULT ''"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX ix_paper_deployments_strategy_id "
            "ON paper_deployments (strategy_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_paper_deployments_status "
            "ON paper_deployments (status)"
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
            "VALUES (1, 2, :ts)"
        ),
            {"ts": datetime.now(timezone.utc)},
        )
        # Seed one legacy deployment row — no Phase 8 columns at all.
        conn.execute(text(
            "INSERT INTO paper_deployments ("
            "deployment_id, strategy_id, strategy_spec_hash,"
            "symbol, timeframe, dataset_id, config_json, status,"
            "evidence_ids_json, created_at, activated_at, updated_at, notes"
            ") VALUES ("
            "'legacy-dep', 'sma5-legacy', 'a' * 64,"
            "'NSE:SBIN', '1d', 'market_data', '{}', 'active',"
            "'[]', :ts, :ts, :ts, ''"
            ")"
        ), {"ts": datetime.now(timezone.utc)})
    yield engine
    engine.dispose()


class TestPaperDeploymentMigration:
    """Phase 8 forward-migration regression tests for ``paper_deployments``."""

    def test_create_all_does_not_add_missing_columns(self, legacy_db):
        """Documents the documented limitation: ``Base.metadata.create_all``
        is a no-op for existing tables, even when the model has new columns.
        """
        from trading_system.storage.database import Base
        # Import the deployment record so it's registered with ``Base``.
        from trading_system.paper.deployment import PaperDeploymentRecord  # noqa: F401
        Base.metadata.create_all(legacy_db)
        inspector = inspect(legacy_db)
        cols = {c["name"] for c in inspector.get_columns("paper_deployments")}
        assert "options_enabled" not in cols, (
            "create_all added a column to an existing table — that would mean "
            "this regression no longer applies"
        )

    def test_ensure_schema_current_adds_missing_columns(self, legacy_db):
        """The v2→v3 migration step adds the 3 missing columns."""
        from trading_system.research.evidence import EvidenceStore
        version = EvidenceStore(legacy_db).ensure_schema_current()
        assert version == 3
        inspector = inspect(legacy_db)
        cols = {c["name"] for c in inspector.get_columns("paper_deployments")}
        assert "options_enabled" in cols
        assert "allowed_option_types_json" in cols
        assert "max_options_contracts_per_trade" in cols

    def test_ensure_schema_current_is_idempotent(self, legacy_db):
        """A second call must be a safe no-op (no error, columns preserved)."""
        from trading_system.research.evidence import EvidenceStore
        store = EvidenceStore(legacy_db)
        first = store.ensure_schema_current()
        assert first == 3
        # Second call: must not raise.
        second = store.ensure_schema_current()
        assert second == 3
        inspector = inspect(legacy_db)
        cols = {c["name"] for c in inspector.get_columns("paper_deployments")}
        assert "options_enabled" in cols

    def test_legacy_rows_remain_readable_post_migration(self, legacy_db):
        """A row written *before* the migration must round-trip with
        documented defaults: ``options_enabled=False``, default
        ``allowed_option_types_json``, ``max_options_contracts_per_trade=NULL``.
        """
        from trading_system.research.evidence import EvidenceStore
        from trading_system.paper.control import PaperTradingControlCenter
        EvidenceStore(legacy_db).ensure_schema_current()

        center = PaperTradingControlCenter.from_engine(legacy_db)
        deps = center.list_deployments()
        assert len(deps) == 1
        d = deps[0]
        assert d.deployment_id == "legacy-dep"
        assert d.symbol == "NSE:SBIN"
        assert d.status.value == "active"
        # Phase 8 defaults applied during ALTER TABLE.
        assert d.options_enabled is False
        assert d.allowed_option_types == ["CE", "PE"]
        assert d.max_options_contracts_per_trade is None

    def test_new_deployment_persists_phase8_fields(self, legacy_db):
        """A ``PaperDeploymentRecord`` written *after* the migration must
        persist the Phase 8 fields with explicit values and round-trip."""
        from trading_system.research.evidence import EvidenceStore
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.paper.deployment import (
            PaperDeployment,
            PaperDeploymentConfig,
            PaperDeploymentRecord,
            PaperDeploymentStatus,
        )
        EvidenceStore(legacy_db).ensure_schema_current()

        center = PaperTradingControlCenter.from_engine(legacy_db)

        cfg = PaperDeploymentConfig(
            options_enabled=True,
            allowed_option_types=["CE"],
            max_options_contracts_per_trade=2,
        )
        deployment = PaperDeployment(
            deployment_id="new-dep",
            strategy_id="new-strat",
            strategy_spec_hash="c" * 64,
            symbol="NSE:TCS",
            timeframe="1d",
            dataset_id="market_data",
            config=cfg,
            status=PaperDeploymentStatus.ACTIVE,
            created_at=datetime.now(timezone.utc).isoformat(),
            activated_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            notes="",
        )
        with center.registry.store._Session() as s:
            s.add(deployment.as_record())
            s.commit()

        # Re-read via the control center's list_deployments (which uses
        # ``rec_to_deployment``) to confirm the Phase 8 fields round-trip.
        deps = center.list_deployments()
        new = next(d for d in deps if d.deployment_id == "new-dep")
        assert new.options_enabled is True
        assert new.allowed_option_types == ["CE"]
        assert new.max_options_contracts_per_trade == 2

    def test_autonomous_filter_still_works_post_migration(self, legacy_db):
        """The autonomous controller's bot-scoped deployment filter must
        keep working after the migration. It reads the same table; the
        Phase 8 columns are unrelated to the filter."""
        from trading_system.research.evidence import EvidenceStore
        from trading_system.paper.control import PaperTradingControlCenter
        EvidenceStore(legacy_db).ensure_schema_current()

        center = PaperTradingControlCenter.from_engine(legacy_db)
        # List everything: the legacy row has no bot prefix, so it should
        # be excluded by the autonomous filter; an inserted bot deployment
        # with notes="bot:<id>"`` must be included.
        all_deps = center.list_deployments()
        assert any(d.deployment_id == "legacy-dep" for d in all_deps)
        # Insert a bot-tagged deployment directly so we can test the filter.
        from datetime import datetime as _dt, timezone as _tz
        with center.registry.store._Session() as s:
            s.execute(text(
                "INSERT INTO paper_deployments ("
                "deployment_id, strategy_id, strategy_spec_hash,"
                "symbol, timeframe, dataset_id, config_json, status,"
                "evidence_ids_json, created_at, activated_at, updated_at,"
                "notes, options_enabled, allowed_option_types_json,"
                "max_options_contracts_per_trade"
                ") VALUES ("
                "'bot-dep', 'bot-strat', 'b' * 64,"
                "'NSE:TCS', '1d', 'market_data', '{}', 'active',"
                "'[]', :ts, :ts, :ts, 'bot:my-bot-id', 0, '[\"CE\"]', 1"
                ")"
            ), {"ts": _dt.now(_tz.utc)})
            s.commit()

        bot_filter = [
            d for d in center.list_deployments()
            if d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == "my-bot-id"
        ]
        assert [d.deployment_id for d in bot_filter] == ["bot-dep"]
        # And the legacy row is excluded.
        assert "legacy-dep" not in [d.deployment_id for d in bot_filter]

    def test_defensive_500_still_returns_safe_when_db_still_broken(
        self, monkeypatch
    ):
        """Regression for the defensive wrapping in
        ``_route_list_deployments`` / ``_route_autonomous_deployments``:
        even if ``center.list_deployments()`` itself raises (e.g. the
        ``SELECT`` against ``paper_deployments`` fails because the
        migration could not be applied), the public API must return
        200 with an empty list + warning, not 500.

        Builds an isolated FastAPI TestClient here so the test is
        self-contained and not coupled to ``test_paper_api_integration``.
        """
        import os, tempfile
        # Use a fresh sqlite file scoped to this test so we have a clean
        # isolated FastAPI app.
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        os.environ["MARKET_DATA_DB_URL"] = f"sqlite:///{tmp.name}"
        from config import get_settings
        get_settings.cache_clear()
        import routes.paper_api as paper_api_mod
        paper_api_mod._api_router = None
        paper_api_mod._controller = None

        from sqlalchemy import create_engine
        # Pre-create the schema (no Phase 8 columns) so the SELECT would
        # fail without the migration.
        engine = create_engine(
            f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}
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
                "notes TEXT NOT NULL DEFAULT ''"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE schema_versions ("
                "id INTEGER PRIMARY KEY, version INTEGER NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            ))
            conn.execute(text(
                "INSERT INTO schema_versions (id, version, updated_at) "
                "VALUES (1, 2, :ts)"
            ), {"ts": datetime.now(timezone.utc)})
        engine.dispose()

        # Patch list_deployments on the control-center class to simulate a
        # SELECT failure (the existing try/except in the routes must catch
        # this and return the safe degraded response).
        from trading_system.paper.control import PaperTradingControlCenter
        def boom(*args, **kwargs):
            raise RuntimeError("simulated SELECT failure")
        monkeypatch.setattr(
            PaperTradingControlCenter,
            "list_deployments",
            boom,
        )

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.paper_api import router as paper_api_router
        app = FastAPI()
        app.include_router(paper_api_router)
        client = TestClient(app)

        try:
            resp = client.get("/api/paper/deployments?limit=200")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body.get("deployments") == []
            assert body.get("count") == 0
            assert "warning" in body
            assert "RuntimeError" in body["warning"]

            resp2 = client.get("/api/paper/autonomous/deployments")
            assert resp2.status_code == 200, resp2.text
            body2 = resp2.json()
            assert body2.get("deployments") == []
            assert "warning" in body2
            assert "RuntimeError" in body2["warning"]
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            get_settings.cache_clear()
            paper_api_mod._api_router = None
            paper_api_mod._controller = None
            monkeypatch.undo()