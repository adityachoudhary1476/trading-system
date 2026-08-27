"""OpenAI-compatible ModelProvider (real client implementation).

IMPLEMENTED but NOT TESTED in this environment:
  * No API key is available on Day 2 (see PROJECT_STATUS / .env.example).
  * `requests` (already a dependency) is used directly to avoid adding the
    openai SDK; the chat/completions JSON contract is standard.
  * It is wired so that when a key + base URL + model are provided via env, it
    will work — but here it raises a clear, controlled error instead of
    fabricating a response.

It expects the model to return STRICT JSON matching MarketView (an explicit
`response_format: json_object` and a system prompt enforce this). Malformed JSON
or a failed schema validation is converted into ModelProviderError — the system
never ingests unvalidated text.

Provider facts (to verify before relying on it):
  * Endpoint: {AI_API_BASE}/chat/completions  (OpenAI-compatible)
  * Auth: bearer token from env var named by AI_API_KEY_ENV (never stored).
  * Cost/rate limits: DEPEND ON THE CHOSEN VENDOR (OpenAI, Together, Groq,
    OpenRouter, a local llama.cpp/Ollama OpenAI shim, etc.). Not assumed free.
  * Local alternative: Ollama exposes an OpenAI-compatible shim at
    http://localhost:11434/v1 — same code path, no external cost, but Ollama is
    not installed in this environment (Day 1 inspection), so it is untested here.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import requests

from ..config import settings, log
from ..models.base import ModelProvider, ModelProviderError
from ..models.snapshot import MarketSnapshot
from ..models.market_view import MarketView


_SYSTEM_PROMPT = (
    "You are a market-analysis assistant. You are NOT a trader and you do NOT "
    "decide position size, leverage, stops, or whether to trade. You only "
    "interpret structured market data and return strict JSON matching this "
    "schema:\n"
    "{\n"
    '  "symbol": str, "timeframe": str,\n'
    '  "market_view": "bullish"|"bearish"|"neutral"|"choppy",\n'
    '  "confidence": float in [0,1] (ANALYTICAL confidence, NOT probability of profit),\n'
    '  "reasoning_summary": str,\n'
    '  "bullish_factors": [str], "bearish_factors": [str],\n'
    '  "risks": [str], "invalidating_conditions": [str]\n'
    "}\n"
    "Return ONLY the JSON object. Use only the data provided."
)


class OpenAICompatibleProvider(ModelProvider):
    name = "openai-compatible"

    def __init__(
        self,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key_env: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        cfg = settings.ai
        self.model = model or cfg.model_name
        self.api_base = (api_base or cfg.api_base).rstrip("/")
        self.api_key_env = api_key_env or cfg.api_key_env or ""
        self.timeout = timeout

    @property
    def is_available(self) -> bool:
        # Available only if a key env var is configured AND the env holds a value.
        if not self.api_key_env:
            return False
        return bool(os.getenv(self.api_key_env))

    def analyze(self, snapshot: MarketSnapshot) -> MarketView:
        if not self.is_available:
            raise ModelProviderError(
                "OpenAICompatibleProvider unavailable: set AI_API_KEY_ENV and the "
                "corresponding environment variable (no key present in Day 2 env)."
            )
        # Defensive re-check.
        api_key = os.getenv(self.api_key_env, "")
        if not api_key:
            raise ModelProviderError("API key not found in environment")

        user_payload = json.dumps(snapshot.to_context_dict(), indent=2, default=str)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        url = f"{self.api_base}/chat/completions"
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            raise ModelProviderError(f"OpenAI-compatible request failed: {e}")

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ModelProviderError(f"model returned non-JSON: {e}")

        # Single validation choke point — arbitrary text cannot pass.
        try:
            return MarketView.from_model_json(data, model=self.model)
        except Exception as e:
            raise ModelProviderError(f"model output failed validation: {e}")
