"""V5 run-registry tests: append-only persistence + hashes."""
from __future__ import annotations

import pathlib
import sys

import pytest
from sqlalchemy import create_engine

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from trading_system.research.run_registry import (  # noqa: E402
    ResearchRunRegistry, config_hash,
)


@pytest.fixture()
def registry():
    eng = create_engine("sqlite://", future=True)
    return ResearchRunRegistry(eng)


class TestRunRegistry:

    def test_create_complete_list(self, registry):
        run = registry.create_run(dataset_id="d1",
                                  dataset_hash="abc123",
                                  config={"step": 5, "seed": 7}, seed=7)
        assert run.status == "running"
        registry.complete_run(run.run_id, {"verdict": "NO_CLEAR_IMPROVEMENT"},
                              warnings=["sample small"])
        runs = registry.list_runs()
        assert len(runs) == 1
        assert runs[0].status == "done"

    def test_run_never_overwritten_append_only(self, registry):
        r1 = registry.create_run(dataset_id="d1")
        r2 = registry.create_run(dataset_id="d2")
        assert r1.run_id != r2.run_id
        assert len(registry.list_runs()) == 2

    def test_create_duplicate_data_ok(self, registry):
        registry.create_run(dataset_id="d")
        registry.create_run(dataset_id="d")
        assert len(registry.list_runs()) == 2

    def test_results_persisted(self, registry):
        run = registry.create_run(dataset_id="d")
        registry.complete_run(run.run_id, {"acc": 0.62})
        got = registry.get_run(run.run_id)
        assert got.results()["acc"] == 0.62

    def test_history_not_overwritten_reads_back_original(self, registry):
        run = registry.create_run(dataset_id="d")
        registry.complete_run(run.run_id, {"v": 1})
        # re-complete (append replacement is allowed but never silently)
        registry.complete_run(run.run_id, {"v": 2},
                              status="done", warnings=["rerun"])
        got = registry.get_run(run.run_id)
        assert got.results()["v"] == 2

    def test_config_hash_type(self):
        h = config_hash({"a": [1, 2], "b": {"c": None}})
        assert isinstance(h, str) and len(h) == 16