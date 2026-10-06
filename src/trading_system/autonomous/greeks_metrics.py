"""Greeks decision-layer counters — decision D6 fallback accounting.

The greeks layer fails OPEN (D1): when greeks are unusable the caller falls
through to legacy behaviour instead of refusing the trade. That is the right
call while the inputs are still untrusted, but it makes a completely broken
layer indistinguishable from a healthy one — no exception, no refused trade,
no error log. These counters are the counterweight: every applicability check
records whether greeks were usable and, when they were not, why, so the
fallback rate can be measured, persisted and alerted on. Without them D1
would hide an outage indefinitely.

Pure accounting: no I/O, no clock, no broker, nothing network-bound. An
instance is an ordinary in-memory object a caller owns; ``default_metrics()``
exists only for callers with nowhere to hang state and is trivially resettable
so tests never inherit another test's counts.
"""

from __future__ import annotations

from typing import Optional


def _bucket(label: Optional[str]) -> str:
    """Fold a caller-supplied label into a stable, JSON-safe bucket key.

    Callers own their reason and limit strings, so nothing is validated or
    invented here — the fold only makes spellings comparable ("No IV",
    "no_iv" and "NO  IV" land in one bucket) and keeps keys safe to persist,
    sort and diff across runs. An absent or blank label becomes "unknown"
    rather than being dropped, because an unexplained fallback is exactly the
    signal D6 cares about most.
    """
    text = str(label or "").strip().lower()
    out: list[str] = []
    pending_sep = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            pending_sep = False
        elif out and not pending_sep:
            out.append("_")
            pending_sep = True
    return "".join(out).strip("_") or "unknown"


class GreeksMetrics:
    """Deterministic counters for one greeks-layer observation window.

    Deliberately not a singleton: each deployment or test owns an instance,
    records primitives (bools, strings), and reads ``snapshot()`` when it
    needs a persistable view of whether the layer actually did anything.
    Counters only — no clock, so two identical record sequences always
    produce identical snapshots.
    """

    def __init__(self) -> None:
        self.reset()

    def record_evaluated(
        self, available: bool, fallback_reason: Optional[str] = None
    ) -> None:
        """Record one applicability check: were usable greeks available?

        The two headline counters are mutually exclusive outcomes of the same
        check, so ``greeks_evaluated + greeks_skipped`` is the number of
        evaluations performed. A reason is only bucketed for a skip: a reason
        attached to usable greeks is not a fallback and must not inflate the
        fallback breakdown that phase 0 gates on.
        """
        if available:
            self._evaluated += 1
            return
        self._skipped += 1
        reason = _bucket(fallback_reason)
        self._fallback_reasons[reason] = self._fallback_reasons.get(reason, 0) + 1

    def record_shadow_verdict(self, would_differ: bool) -> None:
        """Record a shadow comparison; only disagreements are counted.

        Agreement is the expected state and carries no signal. A non-zero
        ``shadow_verdict_diff`` is precisely the "the layer would have traded
        differently" evidence shadow mode exists to collect before any phase
        is allowed to change behaviour.
        """
        if would_differ:
            self._shadow_diff += 1

    def record_strike_selected(self, by: str) -> None:
        """Record which selector chose the strike (e.g. legacy price offset
        versus target delta), so phase 2's real-world effect is measurable."""
        key = _bucket(by)
        self._strike_by[key] = self._strike_by.get(key, 0) + 1

    def record_sizing(self, bound_by: Optional[str]) -> None:
        """Record which clamp bounded the size, or ``"none"`` if none did.

        ``None`` is a meaningful answer — the risk budget itself set the size
        — so it gets its own bucket instead of being lumped into "unknown",
        which is reserved for labels the caller failed to supply.
        """
        key = "none" if bound_by is None else _bucket(bound_by)
        self._sizing_by[key] = self._sizing_by.get(key, 0) + 1

    def record_limit_blocked(self, limit_name: str) -> None:
        """Record a portfolio greek limit that refused an increase, bucketed
        by the limit that fired, so phase 4 refusals are attributable."""
        key = _bucket(limit_name)
        self._limits_blocked[key] = self._limits_blocked.get(key, 0) + 1

    def fallback_rate(self) -> float:
        """Share of recorded evaluations that fell back to legacy behaviour.

        Computed as skipped / evaluated, where evaluated is every evaluation
        recorded (``greeks_evaluated + greeks_skipped``), so the value lives
        in [0, 1] and a window in which greeks were *never* usable reads 1.0
        rather than silence — D6 exists precisely so total failure is
        reportable. Returns 0.0 when nothing has been evaluated: the metric
        must never divide by zero, because an empty window is a state to
        report, not an exception to raise.
        """
        evaluated = self._evaluated + self._skipped
        if evaluated == 0:
            return 0.0
        return self._skipped / evaluated

    def snapshot(self) -> dict:
        """Plain, JSON-serialisable view of every counter.

        Keys are emitted in sorted order (nested dicts included) so two
        snapshots of the same state compare equal, serialise byte-identically
        and diff cleanly against a previously persisted snapshot. Safe to log,
        persist or hand to a dashboard: ints, floats and strings only.
        """
        return {
            "greek_limit_blocked": dict(sorted(self._limits_blocked.items())),
            "greeks_evaluated": self._evaluated,
            "greeks_fallback_reason": dict(sorted(self._fallback_reasons.items())),
            "greeks_skipped": self._skipped,
            "shadow_verdict_diff": self._shadow_diff,
            "sizing_bound_by": dict(sorted(self._sizing_by.items())),
            "strike_selected_by": dict(sorted(self._strike_by.items())),
        }

    def reset(self) -> None:
        """Return to the post-construction state.

        Exists so a long-lived instance can start a fresh observation window
        and so tests can prove counters never leak between cases.
        """
        self._evaluated = 0
        self._skipped = 0
        self._fallback_reasons: dict[str, int] = {}
        self._shadow_diff = 0
        self._strike_by: dict[str, int] = {}
        self._sizing_by: dict[str, int] = {}
        self._limits_blocked: dict[str, int] = {}


_default_metrics = GreeksMetrics()


def default_metrics() -> GreeksMetrics:
    """Process-wide metrics instance for callers that carry no state of their
    own. Optional — tests and stateful callers build their own instead."""
    return _default_metrics


def reset_default_metrics() -> None:
    """Empty the shared instance so counters cannot leak across tests."""
    _default_metrics.reset()
