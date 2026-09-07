"""Phase 8C — Current option premium quote provider.

Fetches real-time option premiums from a read-only Upstox market-data
endpoint, binding each quote to the exact ``Instrument`` identity established
by Phase 8B discovery (``CurrentOptionDiscoverer``).

This is the bridge between "we know which exact option contract exists"
(Phase 8B) and "we know its current market premium" (Phase 8C).

Design principles
-----------------
  * **Exact contract identity** — the quote belongs to the *exact* selected
    ``Instrument`` (same underlying, strike, expiry, CE/PE).  The Upstox
    instrument token / ``provider_symbol`` is used as the lookup key.
  * **Fail-closed** — every failure mode (auth, API, malformed, missing LTP,
    non-positive premium, future-dated, stale) returns ``None``.  The caller
    must treat ``None`` as "do not trade".
  * **Read-only** — only ``GET /market-quote/quotes``.  No order-placement API
    is ever called.  No broker credentials are used for execution.
  * **No synthetic pricing** — the premium is the *actual* Upstox LTP, never
    computed from a model.
  * **Timestamp + freshness** — the quote carries a provider timestamp and a
    ``fetched_at`` so the caller can validate freshness using the project's
    existing market-data freshness configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .instruments import Instrument, InstrumentType
from .live_market_state import normalize_timestamp_ms
from .upstox import (
    UpstoxMarketDataProvider,
    UpstoxAPIError,
    UpstoxAuthError,
    UpstoxNetworkError,
)


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


@dataclass
class OptionQuote:
    """Current market quote for an exact option contract."""

    instrument: Instrument
    ltp: float
    timestamp: datetime
    fetched_at: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    oi: Optional[float] = None
    volume: Optional[float] = None
    raw: Optional[dict] = None
    source_symbol: str = ""

    @property
    def age_seconds(self) -> float:
        return (self.fetched_at - self.timestamp).total_seconds()

    @property
    def contract_id(self) -> str:
        return self.instrument.contract_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument.contract_id,
            "symbol": self.instrument.key,
            "provider_symbol": self.source_symbol,
            "ltp": round(self.ltp, 4),
            "bid": round(self.bid, 4) if self.bid is not None else None,
            "ask": round(self.ask, 4) if self.ask is not None else None,
            "oi": self.oi,
            "volume": self.volume,
            "timestamp": self.timestamp.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "age_seconds": round(self.age_seconds, 3),
        }


class CurrentOptionQuoteProvider:
    """Fetches current option premiums from Upstox market data (read-only).

    The provider takes the exact ``Instrument`` selected by
    ``CurrentOptionDiscoverer`` and returns its current LTP/premium with a
    timestamp for freshness validation.

    Usage::

        provider = CurrentOptionQuoteProvider(upstox_md_provider)
        quote = provider.get_quote(instrument)   # → OptionQuote | None
    """

    #: Maximum acceptable quote age before the quote is considered stale.
    MAX_QUOTE_AGE_SECONDS: float = 300.0  # 5 minutes

    def __init__(
        self,
        provider: UpstoxMarketDataProvider,
        max_quote_age_seconds: float = MAX_QUOTE_AGE_SECONDS,
    ) -> None:
        self._provider = provider
        self._max_age = float(max_quote_age_seconds)

    @property
    def is_authenticated(self) -> bool:
        return self._provider.is_authenticated

    def get_quote(self, instrument: Instrument) -> Optional[OptionQuote]:
        """Fetch the current option premium for the exact ``Instrument``.

        Returns ``OptionQuote`` on success, ``None`` on any failure.
        Never raises — fail-closed by design.
        """
        if not self._provider.is_authenticated:
            return None

        if instrument.instrument_type not in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
            return None

        upstox_symbol = self._resolve_upstox_symbol(instrument)
        if upstox_symbol is None:
            return None

        data = self._fetch_quote(upstox_symbol)
        if data is None:
            return None

        quote_data = self._extract_quote_record(data, upstox_symbol)
        if quote_data is None:
            return None

        ltp = _to_float(quote_data.get("last_price"))
        if ltp is None or ltp <= 0:
            return None

        timestamp = self._parse_timestamp(quote_data)
        if timestamp is None:
            return None

        fetched_at = datetime.now(timezone.utc)

        if timestamp > fetched_at + timedelta(seconds=60):
            return None

        bid = _to_float(
            quote_data.get("buy_price")
            or quote_data.get("bid")
            or quote_data.get("bid_price")
        )
        ask = _to_float(
            quote_data.get("sell_price")
            or quote_data.get("ask")
            or quote_data.get("ask_price")
        )
        oi = _to_float(
            quote_data.get("oi")
            or quote_data.get("open_interest")
        )
        vol = _to_float(
            quote_data.get("volume")
            or quote_data.get("traded_volume")
        )

        return OptionQuote(
            instrument=instrument,
            ltp=ltp,
            timestamp=timestamp,
            fetched_at=fetched_at,
            bid=bid,
            ask=ask,
            oi=oi,
            volume=vol,
            raw=quote_data,
            source_symbol=upstox_symbol,
        )

    def is_fresh(
        self,
        quote: Optional[OptionQuote],
        max_age_seconds: Optional[float] = None,
    ) -> bool:
        """Check whether a quote is fresh enough to use for trading."""
        if quote is None:
            return False
        max_age = max_age_seconds if max_age_seconds is not None else self._max_age
        return quote.age_seconds <= max_age

    # ------------------------------------------------------------------ #
    # Symbol resolution
    # ------------------------------------------------------------------ #

    def _resolve_upstox_symbol(self, instrument: Instrument) -> Optional[str]:
        """Resolve the exact Upstox instrument key for ``instrument``.

        Priority:
          1. ``instrument.provider_symbol`` — set by ``UpstoxInstrumentDiscovery``
             from the option-chain response's ``instrument_key``.
          2. ``provider._upstox_symbol(instrument.key)`` — registry-based
             fallback (handles symbols already in the Upstox format).
        """
        if instrument.provider_symbol:
            return instrument.provider_symbol
        try:
            return self._provider._upstox_symbol(instrument.key)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # API communication
    # ------------------------------------------------------------------ #

    def _fetch_quote(self, upstox_symbol: str) -> Optional[dict]:
        """Call the Upstox read-only quote endpoint.

        Returns the raw response dict or ``None`` on any failure.
        """
        try:
            return self._provider._get(
                "/market-quote/quotes",
                params={"symbol": upstox_symbol},
            )
        except (UpstoxAuthError, UpstoxAPIError, UpstoxNetworkError):
            return None
        except Exception:
            return None

    @staticmethod
    def _extract_quote_record(data: dict, upstox_symbol: str) -> Optional[dict]:
        """Extract the per-instrument quote record from the API response.

        Handles both keyed (``data["data"][symbol]``) and bare-response shapes.
        """
        if not isinstance(data, dict):
            return None

        data_section = data.get("data")
        if isinstance(data_section, dict):
            record = data_section.get(upstox_symbol)
            if isinstance(record, dict):
                return record
            for v in data_section.values():
                if isinstance(v, dict) and v.get("last_price") is not None:
                    return v
        if isinstance(data_section, list):
            for item in data_section:
                if isinstance(item, dict) and item.get("last_price") is not None:
                    return item
        return None

    @staticmethod
    def _parse_timestamp(quote_data: dict) -> Optional[datetime]:
        """Parse a provider timestamp into a tz-aware UTC datetime.

        Uses the project's existing ``normalize_timestamp_ms`` for epoch
        validation, then falls back to ISO parsing.
        """
        ts_raw = (
            quote_data.get("last_traded_timestamp")
            or quote_data.get("timestamp")
            or quote_data.get("ts")
            or quote_data.get("server_timestamp")
            or quote_data.get("exchange_timestamp")
        )
        if ts_raw is None:
            return None

        try:
            ms = normalize_timestamp_ms(ts_raw, now_ms=None)
        except Exception:
            ms = None

        if ms is not None:
            try:
                return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            except (ValueError, OSError):
                return None

        if isinstance(ts_raw, str):
            s = ts_raw.strip()
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z",
            ):
                try:
                    dt = datetime.strptime(s, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc)
                except ValueError:
                    continue
        return None
