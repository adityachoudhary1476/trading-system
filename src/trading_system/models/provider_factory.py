"""Factory for ModelProvider selection (decoupled from concrete classes)."""
from __future__ import annotations

from typing import Optional

from .base import ModelProvider
from .local import LocalRuleModel
from .openai_compatible import OpenAICompatibleProvider


def get_model_provider(name: Optional[str] = None, **kwargs) -> ModelProvider:
    name = (name or "local").lower()
    if name in ("local", "local-rule", "rule"):
        return LocalRuleModel(**kwargs)
    if name in ("openai", "openai-compatible", "openaicompatible"):
        return OpenAICompatibleProvider(**kwargs)
    raise ValueError(f"Unknown model provider: {name}")
