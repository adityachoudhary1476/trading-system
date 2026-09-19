"""Phase 1 — Upstox option-chain snapshot provider.

Fetches, validates, and persists Upstox option-chain snapshots for NIFTY.
Reuses ``UpstoxInstrumentDiscovery`` for HTTP + symbol resolution — no new
API client or auth path is introduced.

Fail-closed contract:
  * Authentication failure  → returns None (logged)
  * Malformed response      → returns None (logged)
  * Empty / no valid rows   → returns None (logged)
  * Data-quality issues are recorded in ``errors`` on the snapshot but
    do NOT cause the call to fail — the caller still receives the chain
    with the valid rows.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..autonomous.options_contract import (
    OptionQuote,
    OptionsChain,
    OptionsChainProvider,
)
from .instruments import OptionType
from .upstox_discovery import UpstoxInstrumentDiscovery

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Field-extraction helpers (robust to v2 vs v3 Upstox response differences)
# --------------------------------------------------------------------------- #
def _extract(d: dict, *names: str) -> Optional[Any]:
    """Return the first non-None value found under any of *names*."""
    for name in names:
        if name in d and d[name] is not None:
            return d[name]
    return None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_expiry(raw: Any) -> str:
    """Convert various expiry formats to ISO ``YYYY-MM-DD``.

    Falls back to returning the stripped string if no known format matches.
    """
    if not raw:
        return ""
    raw = str(raw).strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _infer_strike_interval(strikes: list[float]) -> float:
    """Most common positive difference between consecutive sorted strikes."""
    if len(strikes) < 2:
        return 0.0
    s = sorted(s for s in (float(x) for x in strikes) if s is not None)
    diffs = [round(s[i + 1] - s[i], 2) for i in range(len(s) - 1) if s[i + 1] > s[i]]
    if not diffs:
        return 0.0
    return Counter(diffs).most_common(1)[0][0]


# --------------------------------------------------------------------------- #
# Normalized row (maps 1:1 to OptionChainRowRecord)
# --------------------------------------------------------------------------- #
@dataclass
class NormalizedOptionRow:
    instrument_key: Optional[str]
    underlying: str
    expiry: str
    strike: float
    option_type: str  # "CE" or "PE"
    ltp: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    oi: Optional[int] = None
    change_oi: Optional[int] = None
    bid_iv: Optional[float] = None
    ask_iv: Optional[float] = None


# --------------------------------------------------------------------------- #
# Snapshot (raw JSON preserved + normalized rows + validation metadata)
# --------------------------------------------------------------------------- #
@dataclass
class OptionChainSnapshot:
    snapshot_id: str
    timestamp: datetime
    provider: str
    underlying: str
    expiry: str
    raw_json: str
    validation_status: str  # "validated" | "degraded" | "invalid"
    rows: list[NormalizedOptionRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ChainFetchResult:
    chain: OptionsChain
    snapshot: OptionChainSnapshot


class _AuthError(Exception):
    """Internal: raised when the Upstox API rejects credentials."""


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class UpstoxOptionChainProvider(OptionsChainProvider):
    """Fetch / validate / persist Upstox option chains.

    Usage (from autonomous_scheduler or PaperTradingControlCenter)::

        discovery = UpstoxInstrumentDiscovery(provider=upstox_provider)
        chain_provider = UpstoxOptionChainProvider(discovery, session_factory=MarketStore._Session)
        chain_provider.get_chain("NIFTY", "2025-01-30")
    """

    MAX_STRIKES = 20

    def __init__(
        self,
        discovery: UpstoxInstrumentDiscovery,
        session_factory=None,
    ):
        self._discovery = discovery
        self._session_factory = session_factory  # Optional[Callable[[], Session]]

    # -- public protocol method --
    def get_chain(self, underlying: str, expiry: str) -> Optional[OptionsChain]:
        symbol = f"NSE:{underlying.upper()}"
        try:
            raw = self._fetch_raw(symbol)
        except _AuthError:
            logger.error("option chain fetch: auth failed for %s", symbol)
            return None
        except Exception:
            logger.exception("option chain fetch failed for %s", symbol)
            return None
        if raw is None:
            return None

        result = self._normalize(raw, symbol, expiry)
        if result is None:
            return None  # malformed — fail closed

        if self._session_factory:
            self._persist(result.snapshot)

        if not result.chain.call_quotes and not result.chain.put_quotes:
            return None  # no usable data — fail closed

        return result.chain

    # -- HTTP fetch (reuses UpstoxInstrumentDiscovery) --
    def _fetch_raw(self, symbol: str) -> Optional[dict]:
        exchange, raw_symbol, instr_type = self._discovery._parse_symbol(symbol)
        # Upstox: GET /v3/market/option-chain?symbol=NSE:NIFTY
        # The _get() helper already prepends the Upstox v3 base URL.
        key = f"{exchange.upper()}:{raw_symbol}"
        resp = self._discovery._provider._get(
            "/market/option-chain",
            params={"symbol": key, "strikecount": self.MAX_STRIKES},
        )
        if resp is None:
            raise _AuthError("empty or None response from Upstox")
        status = str(resp.get("status", "success")).strip().lower()
        if status in ("unauthorized", "error"):
            errors = resp.get("errors", [])
            raise _AuthError(str(errors))
        return resp

    # -- normalize + validate --
    def _normalize(self, resp: dict, symbol: str, expiry: str) -> Optional[ChainFetchResult]:
        data = resp.get("data") or resp
        chain = _extract(data, "optionChain", "option_chain", "records")
        if not isinstance(chain, list) or not chain:
            return None  # malformed — fail closed

        underlying_clean = symbol.replace("NSE:", "")

        # Determine which expiry to use
        req_expiry = _normalize_expiry(expiry) if expiry else ""
        if not req_expiry:
            # Pick the first expiry in the chain (Upstox returns weekly first usually)
            for entry in chain:
                if isinstance(entry, dict):
                    e = _normalize_expiry(
                        _extract(entry, "expiry", "expiryDate", "expiry_date", "expi")
                        or ""
                    )
                    if e:
                        req_expiry = e
                        break

        # Spot price
        spot_f = _to_float(
            _extract(data, "spotPrice", "spot_price", "underlyingSpot", "spot")
        )
        if spot_f is None or spot_f <= 0:
            spot_f = 0.0

        rows: list[NormalizedOptionRow] = []
        errors: list[str] = []
        call_quotes: dict[float, OptionQuote] = {}
        put_quotes: dict[float, OptionQuote] = {}
        seen_expiries: set[str] = set()

        for entry in chain:
            if not isinstance(entry, dict):
                continue

            strike = _to_float(_extract(entry, "strikePrice", "strike_price", "strike"))
            if strike is None:
                errors.append("entry missing strikePrice — skipped")
                continue

            entry_expiry_raw = _extract(entry, "expiry", "expiryDate", "expiry_date", "expi")
            entry_expiry = _normalize_expiry(entry_expiry_raw) if entry_expiry_raw else ""
            seen_expiries.add(entry_expiry)

            # Filter: if a specific expiry was requested, skip non-matching entries
            if req_expiry and entry_expiry != req_expiry:
                continue

            for leg_key, ot in (("ce", OptionType.CE), ("pe", OptionType.PE),
                                ("callOption", OptionType.CE), ("putOption", OptionType.PE)):
                leg = entry.get(leg_key)
                if leg is None:
                    continue
                if not isinstance(leg, dict):
                    continue

                instrument_key = _extract(leg, "instrumentKey", "instrument_key", "key")
                if not instrument_key:
                    errors.append(
                        f"missing instrument_key for strike={strike} {ot.value}"
                    )
                    continue

                ltp = _to_float(_extract(leg, "ltp", "lastPrice", "last_price", "last"))
                bid = _to_float(_extract(leg, "bidPrice", "bid", "bid_price"))
                ask = _to_float(_extract(leg, "askPrice", "ask", "ask_price"))
                volume = _to_int(_extract(leg, "volume", "tradedVolume", "traded_volume", "vol"))
                oi = _to_int(_extract(leg, "oi", "openInterest", "open_interest"))
                change_oi = _to_int(_extract(leg, "changeOI", "change_oi", "changeinOI", "change_oi"))
                bid_iv = _to_float(_extract(leg, "bidIv", "bid_iv", "bidIV", "iv_bid"))
                ask_iv = _to_float(_extract(leg, "askIv", "ask_iv", "askIV", "iv_ask"))

                # --- validation rules (Step 5) ---
                if volume is not None and volume < 0:
                    errors.append(
                        f"negative volume for {instrument_key}: {volume}"
                    )
                if oi is not None and oi < 0:
                    errors.append(
                        f"negative OI for {instrument_key}: {oi}"
                    )
                if bid is not None and ask is not None and bid > ask:
                    errors.append(
                        f"bid > ask for {instrument_key}: bid={bid} ask={ask}"
                    )
                if ltp is None:
                    errors.append(f"missing ltp for {instrument_key}")
                if strike is None:
                    errors.append(f"missing strike for {instrument_key}")

                row = NormalizedOptionRow(
                    instrument_key=instrument_key,
                    underlying=underlying_clean,
                    expiry=req_expiry,
                    strike=strike if strike is not None else 0.0,
                    option_type=ot.value,
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    volume=volume,
                    oi=oi,
                    change_oi=change_oi,
                    bid_iv=bid_iv,
                    ask_iv=ask_iv,
                )
                rows.append(row)

                quote = OptionQuote(
                    strike=strike if strike is not None else 0.0,
                    bid=bid if bid is not None else 0.0,
                    ask=ask if ask is not None else 0.0,
                    last=ltp,
                    volume=volume or 0,
                    open_interest=oi or 0,
                    # Extended fields
                    instrument_key=instrument_key,
                    expiry=entry_expiry or req_expiry,
                    option_type=ot.value,
                    ltp=ltp,
                    change_oi=change_oi,
                    bid_iv=bid_iv,
                    ask_iv=ask_iv,
                )
                if ot == OptionType.CE:
                    call_quotes[quote.strike] = quote
                else:
                    put_quotes[quote.strike] = quote

        if spot_f == 0.0:
            errors.append("missing spot price from response")

        interval = _infer_strike_interval(list(call_quotes.keys()))

        chain_obj = OptionsChain(
            underlying=underlying_clean,
            expiry=req_expiry,
            spot_price=spot_f,
            strike_interval=interval,
            call_quotes=call_quotes,
            put_quotes=put_quotes,
        )

        validation_status = "invalid" if errors else "validated"
        # If there are errors but also valid rows, mark as "degraded"
        if errors and (call_quotes or put_quotes):
            validation_status = "degraded"

        snapshot = OptionChainSnapshot(
            snapshot_id=uuid.uuid4().hex,
            timestamp=datetime.now(timezone.utc),
            provider="upstox",
            underlying=underlying_clean,
            expiry=req_expiry,
            raw_json=json.dumps(resp),
            validation_status=validation_status,
            rows=rows,
            errors=errors,
        )

        return ChainFetchResult(chain=chain_obj, snapshot=snapshot)

    # -- persist --
    def _persist(self, snapshot: OptionChainSnapshot) -> None:
        from ..paper.option_chain_models import persist_snapshot

        try:
            with self._session_factory() as session:
                persist_snapshot(
                    session,
                    snapshot_id=snapshot.snapshot_id,
                    timestamp=snapshot.timestamp,
                    provider=snapshot.provider,
                    underlying=snapshot.underlying,
                    expiry=snapshot.expiry,
                    raw_json=snapshot.raw_json,
                    validation_status=snapshot.validation_status,
                    rows=snapshot.rows,
                )
                session.commit()
        except Exception:
            logger.exception(
                "failed to persist option chain snapshot %s", snapshot.snapshot_id
            )

    # -- probe helper for _inspect_options_providers --
    def is_healthy(self) -> bool:
        """Quick connectivity + auth check without persisting a snapshot."""
        try:
            raw = self._fetch_raw("NSE:NIFTY")
            return raw is not None
        except Exception:
            logger.exception("UpstoxOptionChainProvider health probe failed")
            return False
