"""Data validation for OHLCV frames.

The validator is intentionally strict: it FAILS LOUDLY. Invalid rows are
rejected (returned separately) and any error-severity problem is surfaced so
the ingestion pipeline can refuse to produce signals from bad data.

Expected input: a pandas DataFrame with at least
    open, high, low, close, volume, timestamp  (symbol/timeframe supplied separately)
Timestamps should be tz-aware UTC; mixed tz or naive/aware mixes are flagged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class Severity(str, Enum):
    ERROR = "error"      # data cannot be trusted; reject rows / abort
    WARNING = "warning"  # suspicious but usable; report only


EXPECTED_NUMERIC = ["open", "high", "low", "close", "volume"]


@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: Severity
    # Indices (into the original frame) of offending rows, if row-specific.
    rows: list[int] = field(default_factory=list)


@dataclass
class ValidationReport:
    valid: pd.DataFrame
    rejected: pd.DataFrame
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when there are no error-severity issues AND no rejected rows."""
        return not any(i.severity == Severity.ERROR for i in self.issues) and len(
            self.rejected
        ) == 0


@dataclass
class DataValidationError(ValueError):
    """Raised when data fails validation and cannot be trusted."""

    report: ValidationReport


def _add_issue(
    issues: list[ValidationIssue],
    code: str,
    message: str,
    severity: Severity,
    rows: Optional[list[int]] = None,
) -> None:
    issues.append(ValidationIssue(code, message, severity, rows or []))


def _expected_interval(timeframe: str) -> Optional[pd.Timedelta]:
    """Map a timeframe string to its nominal bar interval."""
    mapping = {
        "1m": pd.Timedelta(minutes=1),
        "5m": pd.Timedelta(minutes=5),
        "15m": pd.Timedelta(minutes=15),
        "30m": pd.Timedelta(minutes=30),
        "1h": pd.Timedelta(hours=1),
        "4h": pd.Timedelta(hours=4),
        "1d": pd.Timedelta(days=1),
        "1w": pd.Timedelta(weeks=1),
        "1M": pd.Timedelta(days=30),
    }
    return mapping.get(timeframe)


def validate_ohlcv(
    df: pd.DataFrame,
    timeframe: str,
    max_gap_factor: float = 5.0,
    reject_on_gap: bool = False,
) -> ValidationReport:
    """Validate an OHLCV DataFrame.

    Parameters
    ----------
    df:
        DataFrame with columns open/high/low/close/volume and a DatetimeIndex
        or a 'timestamp' column.
    timeframe:
        Bar interval (e.g. '1d'). Used for gap detection.
    max_gap_factor:
        A gap larger than max_gap_factor * expected_interval is flagged as an
        abnormal gap (warning by default).
    reject_on_gap:
        If True, rows participating in an abnormal gap are also rejected.

    Returns
    -------
    ValidationReport with `valid`, `rejected`, and `issues`.
    """
    issues: list[ValidationIssue] = []

    if df is None or len(df) == 0:
        _add_issue(issues, "EMPTY", "Input frame is empty", Severity.ERROR)
        return ValidationReport(pd.DataFrame(), pd.DataFrame(), issues)

    # --- Normalize to a plain RangeIndex + a single 'timestamp' column ------
    frame = df.copy()
    if "timestamp" in frame.columns:
        # Possible collision with a DatetimeIndex named 'timestamp'; drop index.
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index(drop=True)
        ts = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    else:
        if not isinstance(frame.index, pd.DatetimeIndex):
            _add_issue(
                issues,
                "NO_TIMESTAMP",
                "Frame has neither a 'timestamp' column nor a DatetimeIndex",
                Severity.ERROR,
            )
            return ValidationReport(pd.DataFrame(), pd.DataFrame(), issues)
        ts = pd.to_datetime(frame.index, utc=True, errors="coerce")
        frame = frame.reset_index(drop=True)
    frame["timestamp"] = ts

    # --- Missing timestamp rows --------------------------------------------
    missing_ts = frame["timestamp"].isna()
    if missing_ts.any():
        idx = missing_ts[missing_ts].index.tolist()
        _add_issue(
            issues,
            "MISSING_TIMESTAMP",
            f"{len(idx)} row(s) have missing/parseable timestamps",
            Severity.ERROR,
            rows=idx,
        )

    # --- Timezone consistency ----------------------------------------------
    tz_aware = pd.DatetimeTZDtype("ns", "UTC")
    if not pd.api.types.is_datetime64_any_dtype(frame["timestamp"]):
        _add_issue(
            issues,
            "BAD_TIMESTAMP_TYPE",
            "Timestamp column is not datetime after parsing",
            Severity.ERROR,
        )

    # --- Required numeric columns present ----------------------------------
    missing_cols = [c for c in EXPECTED_NUMERIC if c not in frame.columns]
    if missing_cols:
        _add_issue(
            issues,
            "MISSING_COLUMNS",
            f"Missing required columns: {missing_cols}",
            Severity.ERROR,
        )
        return ValidationReport(pd.DataFrame(), pd.DataFrame(), issues)

    # --- Missing values (NaN) in required fields ---------------------------
    for col in EXPECTED_NUMERIC:
        nan_mask = frame[col].isna()
        if nan_mask.any():
            idx = nan_mask[nan_mask].index.tolist()
            _add_issue(
                issues,
                "MISSING_VALUE",
                f"Column '{col}' has {len(idx)} NaN value(s)",
                Severity.ERROR,
                rows=idx,
            )

    # --- Impossible / non-positive prices ----------------------------------
    for col in ["open", "high", "low", "close"]:
        bad = frame[col] <= 0
        if bad.any():
            idx = bad[bad].index.tolist()
            _add_issue(
                issues,
                "IMPOSSIBLE_PRICE",
                f"Column '{col}' has non-positive value(s)",
                Severity.ERROR,
                rows=idx,
            )
    neg_vol = frame["volume"] < 0
    if neg_vol.any():
        idx = neg_vol[neg_vol].index.tolist()
        _add_issue(
            issues,
            "NEGATIVE_VOLUME",
            f"Volume has {len(idx)} negative value(s)",
            Severity.ERROR,
            rows=idx,
        )

    # --- Invalid OHLC relationships ----------------------------------------
    ohlc_bad = (
        (frame["high"] < frame["low"])
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    )
    if ohlc_bad.any():
        idx = ohlc_bad[ohlc_bad].index.tolist()
        _add_issue(
            issues,
            "BAD_OHLC",
            f"{len(idx)} row(s) violate high>=low>=open/close relationships",
            Severity.ERROR,
            rows=idx,
        )

    # --- Ordering: timestamps must be strictly increasing ------------------
    sorted_ok = frame["timestamp"].is_monotonic_increasing and not (
        frame["timestamp"].duplicated().any()
    )
    if not sorted_ok:
        # Mark duplicated timestamps as invalid.
        dup = frame["timestamp"].duplicated(keep="first")
        # Mark out-of-order rows: a row is out of order if it is <= the max
        # timestamp seen before it.
        prev_max = frame["timestamp"].cummax().shift(1)
        out_of_order = frame["timestamp"] <= prev_max
        bad_order = dup | out_of_order.fillna(False)
        idx = bad_order[bad_order].index.tolist()
        _add_issue(
            issues,
            "ORDERING",
            f"{len(idx)} row(s) have duplicate or non-increasing timestamps",
            Severity.ERROR,
            rows=idx,
        )

    # --- Abnormal gaps -----------------------------------------------------
    interval = _expected_interval(timeframe)
    if interval is not None and len(frame) > 1:
        diffs = frame["timestamp"].diff().dropna()
        if not diffs.empty:
            big = diffs > (interval * max_gap_factor)
            if big.any():
                n_gaps = int(big.sum())
                # rows immediately after each big gap
                gap_rows = diffs[big].index.tolist()
                _add_issue(
                    issues,
                    "ABNORMAL_GAP",
                    f"{n_gaps} gap(s) larger than {max_gap_factor}x the "
                    f"{timeframe} interval detected",
                    Severity.WARNING if not reject_on_gap else Severity.ERROR,
                    rows=gap_rows,
                )

    # --- Build valid / rejected subsets ------------------------------------
    error_rows: set[int] = set()
    for iss in issues:
        if iss.severity == Severity.ERROR and iss.rows:
            error_rows.update(iss.rows)
    if reject_on_gap:
        warn_gap = next(
            (i for i in issues if i.code == "ABNORMAL_GAP"), None
        )
        if warn_gap:
            error_rows.update(warn_gap.rows)

    all_idx = set(frame.index)
    valid_idx = sorted(all_idx - error_rows)
    rejected_idx = sorted(error_rows & all_idx)

    valid = frame.loc[valid_idx].sort_values("timestamp").reset_index(drop=True)
    rejected = frame.loc[rejected_idx].sort_values("timestamp").reset_index(drop=True)

    return ValidationReport(valid=valid, rejected=rejected, issues=issues)


def validate_contract_identity(instrument) -> list[ValidationIssue]:
    """Provider-independent sanity check of derivative contract metadata.

    Catches malformed contract identity BEFORE any data is fetched/stored. This is
    a structural check (not OHLC); it complements `validate_ohlcv`. Legitimate
    derivative behavior (e.g. wide strike spacing, weekly expiries) is never
    rejected — we only flag impossible metadata.

    Parameters
    ----------
    instrument: trading_system.india.instruments.Instrument

    Returns
    -------
    list[ValidationIssue]  (empty when the contract identity is valid)
    """
    issues: list[ValidationIssue] = []
    itype = getattr(instrument, "instrument_type", None)
    if itype is None:
        return issues

    from ..india.instruments import InstrumentType  # local import to avoid cycle

    is_deriv = itype in (
        InstrumentType.FUTURE, InstrumentType.OPTION_CE, InstrumentType.OPTION_PE
    )
    if not is_deriv:
        return issues

    if not getattr(instrument, "underlying", None):
        _add_issue(
            issues, "CONTRACT_META", "Derivative missing underlying", Severity.ERROR,
        )
    expiry = getattr(instrument, "expiry", None)
    if expiry:
        try:
            exp = __import__("datetime").date.fromisoformat(expiry)
            if exp < __import__("datetime").date.today():
                # Expired contracts are not necessarily invalid to *represent*, but
                # fetching live history for them is almost always a user error; warn.
                _add_issue(
                    issues, "EXPIRED_CONTRACT",
                    f"Contract expiry {expiry} is in the past", Severity.WARNING,
                )
        except (ValueError, TypeError):
            _add_issue(
                issues, "BAD_EXPIRY", f"Expiry {expiry!r} is not ISO YYYY-MM-DD",
                Severity.ERROR,
            )
    else:
        _add_issue(issues, "CONTRACT_META", "Derivative missing expiry", Severity.ERROR)

    if itype in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
        strike = getattr(instrument, "strike", None)
        if strike is None or strike <= 0:
            _add_issue(
                issues, "BAD_STRIKE", f"Option strike {strike!r} is missing/invalid",
                Severity.ERROR,
            )
        ot = getattr(instrument, "option_type", None)
        if ot not in ("CE", "PE"):
            _add_issue(
                issues, "BAD_OPTION_TYPE", f"Option type {ot!r} must be CE/PE",
                Severity.ERROR,
            )
    return issues


def assert_valid(report: ValidationReport) -> pd.DataFrame:
    """Return the valid frame, raising DataValidationError otherwise."""
    if not report.ok:
        raise DataValidationError(report)
    return report.valid
