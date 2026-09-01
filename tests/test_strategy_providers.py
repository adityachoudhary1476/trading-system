"""Phase 13 tests: AI strategy proposal providers.

DeterministicStrategyProvider must work offline with NO API key. The
OpenAI-compatible strategy provider must fail safely on: missing key,
non-JSON output, malformed specs, and transport errors — a bad LLM response
can NEVER become a spec. All network interaction is monkeypatched (offline).
"""
from __future__ import annotations

import json

import pytest

from trading_system.models.base import ModelProviderError
from trading_system.models.openai_compatible import OpenAICompatibleProvider
from trading_system.research.backtester import BacktestConfig
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    OpenAICompatibleStrategyProvider,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.spec import StrategySpec
from trading_system.research.strategy_lab.validation import validate_spec


CTX = GenerationContext(symbol="NSE:SBIN", timeframe="1d", rows=300)


# --------------------------------------------------------------------------- #
# Deterministic mock provider
# --------------------------------------------------------------------------- #
def test_deterministic_provider_available_offline():
    provider = DeterministicStrategyProvider()
    assert provider.is_available is True
    assert provider.catalog_size >= 4


def test_deterministic_provider_returns_valid_specs_for_all_variants():
    provider = DeterministicStrategyProvider()
    for i in range(provider.catalog_size):
        spec = provider.generate_strategy(CTX.with_variant(i))
        assert isinstance(spec, StrategySpec)
        assert validate_spec(spec) == []
        assert spec.symbol == "NSE:SBIN"
        assert spec.timeframe == "1d"


def test_deterministic_provider_is_deterministic():
    provider = DeterministicStrategyProvider()
    for i in range(provider.catalog_size):
        s1 = provider.generate_strategy(CTX.with_variant(i))
        s2 = provider.generate_strategy(CTX.with_variant(i))
        assert s1.model_dump(mode="json") == s2.model_dump(mode="json")


def test_deterministic_provider_wraps_around_catalog():
    provider = DeterministicStrategyProvider()
    a = provider.generate_strategy(CTX.with_variant(0))
    b = provider.generate_strategy(CTX.with_variant(provider.catalog_size))
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_deterministic_provider_is_a_strategy_proposal_provider():
    assert isinstance(DeterministicStrategyProvider(), StrategyProposalProvider)


def test_generation_context_from_dataset():
    import pandas as pd
    from trading_system.research.dataset import HistoricalDataset

    idx = pd.date_range("2024-01-01", periods=100, freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=idx,
    )
    ds = HistoricalDataset(symbol="NSE:X", timeframe="1d", data=df)
    ctx = GenerationContext.from_dataset(ds, variant_index=2)
    assert ctx.symbol == "NSE:X"
    assert ctx.rows == 100
    assert ctx.variant_index == 2
    assert ctx.date_start is not None
    # variant propagation must be non-mutating
    other = ctx.with_variant(3)
    assert other.variant_index == 3 and ctx.variant_index == 2


def test_generation_context_has_no_execution_surface():
    payload = json.dumps(CTX.as_dict())
    assert "order" not in payload
    assert "broker" not in payload


# --------------------------------------------------------------------------- #
# OpenAI-compatible strategy provider (extends the existing client)
# --------------------------------------------------------------------------- #
def _strategy_provider(monkeypatch, key="TEST_KEY"):
    monkeypatch.setenv("TEST_KEY", key)
    return OpenAICompatibleStrategyProvider(
        model="test-model", api_base="http://localhost:9/v1", api_key_env="TEST_KEY"
    )


def test_openai_strategy_provider_subclasses_existing_provider():
    assert issubclass(OpenAICompatibleStrategyProvider, OpenAICompatibleProvider)


def test_openai_strategy_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    provider = OpenAICompatibleStrategyProvider(
        model="m", api_base="http://x/v1", api_key_env="TEST_KEY"
    )
    assert provider.is_available is False
    with pytest.raises(ModelProviderError):
        provider.generate_strategy(CTX)


def _valid_llm_spec_json() -> str:
    return json.dumps({
        "name": "LLM sma pullback",
        "description": "Buy above SMA20.",
        "symbol": "NSE:SBIN",
        "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": {
            "type": "comparison",
            "left": {"kind": "field", "field": "close"},
            "op": ">",
            "right": {"kind": "indicator", "indicator": "sma_20"},
        },
        "risk": {"stop_loss_pct": 0.05},
    })


def test_openai_strategy_provider_valid_output(monkeypatch):
    provider = _strategy_provider(monkeypatch)
    monkeypatch.setattr(provider, "_post_chat", lambda s, u: _valid_llm_spec_json())
    spec = provider.generate_strategy(CTX)
    assert isinstance(spec, StrategySpec)
    assert spec.name == "LLM sma pullback"
    assert spec.generated_by == "test-model"
    assert validate_spec(spec) == []


def test_openai_strategy_provider_malformed_json_fails_safely(monkeypatch):
    provider = _strategy_provider(monkeypatch)
    monkeypatch.setattr(provider, "_post_chat", lambda s, u: "not json at all {")
    with pytest.raises(ModelProviderError):
        provider.generate_strategy(CTX)


def test_openai_strategy_provider_invalid_spec_fails_validation(monkeypatch):
    provider = _strategy_provider(monkeypatch)
    # Unknown indicator + code payload in description: must be rejected.
    bad = json.loads(_valid_llm_spec_json())
    bad["indicators"] = [{"name": "quantum_oscillator", "params": {}}]
    bad["description"] = "import os; take over"
    monkeypatch.setattr(provider, "_post_chat", lambda s, u: json.dumps(bad))
    with pytest.raises(ModelProviderError):
        provider.generate_strategy(CTX)


def test_openai_strategy_provider_non_object_output_fails(monkeypatch):
    provider = _strategy_provider(monkeypatch)
    monkeypatch.setattr(provider, "_post_chat", lambda s, u: '"just a string"')
    with pytest.raises(ModelProviderError):
        provider.generate_strategy(CTX)


def test_openai_strategy_provider_transport_failure_wrapped(monkeypatch):
    provider = _strategy_provider(monkeypatch)

    def boom(system_prompt, user_payload):
        raise ModelProviderError("OpenAI-compatible request failed: timeout")

    monkeypatch.setattr(provider, "_post_chat", boom)
    with pytest.raises(ModelProviderError):
        provider.generate_strategy(CTX)


def test_openai_strategy_provider_system_prompt_forbids_code():
    from trading_system.research.strategy_lab.providers import _STRATEGY_SYSTEM_PROMPT

    assert "NO Python" in _STRATEGY_SYSTEM_PROMPT
    assert "NO code" in _STRATEGY_SYSTEM_PROMPT


def test_merged_config_shorts_require_both_permissions():
    provider = DeterministicStrategyProvider()
    short_spec = provider.generate_strategy(CTX.with_variant(6))  # long-short entry
    assert short_spec.risk.allow_short is True
    cfg = merged_backtest_config(short_spec, BacktestConfig())
    # Base BacktestConfig defaults to allow_short=False: engine CANNOT grant it.
    assert cfg.risk.allow_short is False

