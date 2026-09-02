"""Upstox V3 instrument-key resolution.

Resolves application symbols (e.g. ``NSE:SBIN``) to the actual Upstox V3
instrument keys required for live market-data subscription.

The V3 feed requires:

* Equities: ``NSE_EQ|<ISIN>``        (e.g. ``NSE_EQ|INE062A01020`` for SBIN)
* Indices : ``NSE_INDEX|<Index Name>`` (e.g. ``NSE_INDEX|Nifty 50``)

This is NOT a string-conversion shortcut. Equity keys are looked up via the
official Upstox Instrument Search API (``/v2/instruments/search``), which
returns the canonical ``instrument_key`` for the security. The resolver:

* Caches resolved keys in memory to avoid repeated API calls.
* Maps a small, well-known set of index names locally to avoid HTTP for
  indices (which are stable; per the official SDK the index key uses the
  Upstox name verbatim, e.g. ``Nifty 50``).
* FAILS CLEARLY (raises ``UnresolvedInstrumentError``) if a symbol cannot
  be resolved. The caller is responsible for not subscribing to invalid
  keys. We deliberately do NOT silently fall back to the trading-symbol
  form (``NSE_EQ|SBIN``) because that produces zero market data (the V3
  feed only recognises ISIN-based keys for equities).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import requests

from .instruments import Instrument, InstrumentType


log = logging.getLogger(__name__)


_UPSTOX_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"


# Well-known NSE indices. Values are the EXACT Upstox V3 ``instrument_key``
# strings (segment + index name) per the official Upstox docs. Equity
# instruments are resolved dynamically (not hardcoded).
_KNOWN_INDICES: dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "NIFTYBANK": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "SENSEX": "BSE_INDEX|Sensex",
    "BANKEX": "BSE_INDEX|Bankex",
}


class UnresolvedInstrumentError(ValueError):
    """Raised when an application symbol cannot be resolved to a Upstox key."""


class UpstoxV3InstrumentResolver:
    """Resolve application symbols to Upstox V3 instrument keys.

    Args:
        access_token: Upstox access token used to call the search API.
        session: Optional ``requests.Session`` (useful for tests).
        timeout: HTTP timeout (seconds) for the search API.
        search_fn: Optional injectable search function. Signature:
            ``(query: str) -> list[dict]`` returning the raw ``data`` array
            from the V2 search API response. Defaults to the live HTTP call.
            The injectable function makes the resolver fully testable
            without any real network call.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
        search_fn: Optional[Callable[[str], list[dict]]] = None,
    ) -> None:
        self._access_token = access_token
        self._session = session
        self._timeout = timeout
        self._search_fn = search_fn
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def resolve(self, application_symbol: str) -> str:
        """Resolve an application symbol to a Upstox V3 instrument key.

        Args:
            application_symbol: Symbol in ``<EXCHANGE>:<NAME>`` form
                (e.g. ``NSE:SBIN``, ``NSE:NIFTY50``, ``NSE:NIFTY 50``),
                or an Upstox V3 instrument key already
                (e.g. ``NSE_EQ|INE062A01020``) which is returned as-is.

        Returns:
            The Upstox V3 ``instrument_key`` (e.g. ``NSE_EQ|INE062A01020``).

        Raises:
            UnresolvedInstrumentError: If the symbol cannot be resolved.
        """
        if not application_symbol or not isinstance(application_symbol, str):
            raise UnresolvedInstrumentError(
                f"Cannot resolve empty or non-string symbol: {application_symbol!r}"
            )

        s = application_symbol.strip()
        if not s:
            raise UnresolvedInstrumentError("Cannot resolve empty symbol")

        # Already an Upstox instrument key? Return as-is.
        if "|" in s and "_" in s.split("|", 1)[0]:
            return s

        with self._lock:
            if s in self._cache:
                return self._cache[s]

        # Split into exchange/symbol if the caller used the app-internal form.
        if ":" in s:
            exchange, raw = s.split(":", 1)
            exchange = exchange.strip().upper()
            raw = raw.strip()
        else:
            exchange, raw = "NSE", s

        # Index fast-path (no network needed).
        if raw.upper() in {k.upper() for k in _KNOWN_INDICES}:
            for k, v in _KNOWN_INDICES.items():
                if k.upper() == raw.upper():
                    with self._lock:
                        self._cache[s] = v
                    return v

        # Equity: look up via the official Upstox search API.
        instrument_key = self._resolve_equity(raw, exchange)
        if instrument_key is None:
            raise UnresolvedInstrumentError(
                f"Could not resolve application symbol {application_symbol!r} "
                f"to a Upstox V3 instrument key. The symbol may be invalid or "
                f"the Upstox search API is unreachable."
            )

        with self._lock:
            self._cache[s] = instrument_key
        return instrument_key

    def _resolve_equity(self, raw_symbol: str, exchange: str) -> Optional[str]:
        """Call the Upstox V2 search API to find the equity instrument key."""
        try:
            results = self._call_search(raw_symbol)
        except Exception as e:
            log.warning("Upstox search API failed for %r: %s", raw_symbol, e)
            return None

        if not results:
            return None

        # Prefer an exact match on the trading symbol for the requested
        # exchange; otherwise take the first NSE_EQ result.
        exact = None
        nse_eq_fallback = None
        for r in results:
            seg = (r.get("segment") or "").upper()
            if seg != "NSE_EQ" and seg != f"{exchange}_EQ":
                continue
            ts = (r.get("trading_symbol") or "").upper()
            if ts == raw_symbol.upper():
                exact = r
                break
            if nse_eq_fallback is None:
                nse_eq_fallback = r

        chosen = exact or nse_eq_fallback
        if chosen is None:
            return None
        return chosen.get("instrument_key")

    def _call_search(self, query: str) -> list[dict]:
        """Invoke the (injectable) search function or the live HTTP API."""
        if self._search_fn is not None:
            return list(self._search_fn(query) or [])

        if not self._access_token:
            return []
        sess = self._session or requests
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        params = {"query": query, "exchanges": "NSE", "segments": "EQ", "records": 5}
        try:
            resp = sess.get(
                _UPSTOX_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            log.warning("Upstox search HTTP error: %s", e)
            return []
        if resp.status_code != 200:
            log.warning("Upstox search returned HTTP %s", resp.status_code)
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return []
        data = payload.get("data") or []
        return [r for r in data if isinstance(r, dict)]

    # ------------------------------------------------------------------
    # Convenience: resolve a normalized ``Instrument`` (used by
    # ``UpstoxMarketDataProvider._upstox_symbol`` callers).
    # ------------------------------------------------------------------
    def resolve_instrument(self, instrument: Instrument) -> str:
        """Resolve a normalized ``Instrument`` to an Upstox V3 key.

        - Index instruments: looked up in the local well-known index map.
        - Equity instruments: looked up via the search API.
        - Derivatives: passes through to ``to_upstox_symbol`` (the legacy
          FYERS-style key is reused; V3 still accepts them).
        """
        if instrument.instrument_type == InstrumentType.INDEX:
            base = instrument.internal.symbol
            for k, v in _KNOWN_INDICES.items():
                if k.upper() == base.upper():
                    return v
            raise UnresolvedInstrumentError(
                f"Unknown index instrument: {instrument.internal.symbol!r}. "
                f"Add it to _KNOWN_INDICES or supply a custom resolver."
            )
        if instrument.instrument_type == InstrumentType.EQUITY:
            return self.resolve(f"{instrument.internal.exchange}:{instrument.internal.symbol}")
        # For derivatives, fall back to the legacy mapping (V3 supports these
        # in the same ``NSE_FO|<token>`` form).
        from .symbol_map import to_upstox_symbol

        return to_upstox_symbol(instrument)
