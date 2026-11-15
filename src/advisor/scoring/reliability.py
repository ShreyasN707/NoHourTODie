"""P(cannot treat | h) and hospital reliability (spec §4, §5).

Beta-style estimate with data-age decay:
    P = (decayed_rejects + prior_rejects) / (decayed_total + prior_total)
Each past accept/reject counts 0.5 ** (age_days / half_life_days), so stale history fades and a
hospital that has started refusing is noticed quickly. If the required capability is missing, P = 1.

Provenance: prior and half-life are PLACEHOLDER (config/advisor.yaml). Outcome history is SIMULATED
until the pre-alert loop runs for real.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from advisor.models import OutcomeEvent
from advisor.provenance import Provenance


def decay_weight(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        raise ValueError("half_life_days must be > 0")
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def decayed_counts(
    events: Iterable[OutcomeEvent], hospital_id: str, now: datetime, half_life_days: float
) -> tuple[float, float]:
    """(decayed rejects, decayed total) for one hospital, ignoring events after `now`."""
    rejects = total = 0.0
    for e in events:
        if e.hospital_id != hospital_id or e.timestamp > now:
            continue
        w = decay_weight((now - e.timestamp).total_seconds() / 86400.0, half_life_days)
        total += w
        if not e.accepted:
            rejects += w
    return rejects, total


class OutcomeLog(list):
    """An OutcomeEvent list that also keeps running decayed counts per hospital.

    Same answers as `decayed_counts` (up to float rounding) but O(1) per query instead of rescanning
    the whole log: each hospital keeps (decayed rejects, decayed total, reference time), and moving
    the reference time forward just multiplies both sums by the decay factor. This is what keeps a
    28-day replay linear instead of quadratic.

    The fast path needs events appended in time order and queries at or after the last event. If
    either is not true, that hospital falls back to an exact scan of its own events.
    """

    def __init__(self, half_life_days: float, events: Iterable[OutcomeEvent] = ()):
        super().__init__()
        if half_life_days <= 0:
            raise ValueError("half_life_days must be > 0")
        self.half_life_days = half_life_days
        self._sums: dict[str, list] = {}  # hospital -> [rejects, total, t_ref]
        self._by_h: dict[str, list[OutcomeEvent]] = {}
        self._exact: set[str] = set()  # hospitals with out-of-order events
        self._provs: dict[str, set[Provenance]] = {}
        self.extend(events)

    def append(self, e: OutcomeEvent) -> None:
        super().append(e)
        self._by_h.setdefault(e.hospital_id, []).append(e)
        self._provs.setdefault(e.hospital_id, set()).add(e.source)
        s = self._sums.get(e.hospital_id)
        if s is None:
            self._sums[e.hospital_id] = [0.0 if e.accepted else 1.0, 1.0, e.timestamp]
            return
        if e.timestamp < s[2]:
            self._exact.add(e.hospital_id)
            return
        f = decay_weight((e.timestamp - s[2]).total_seconds() / 86400.0, self.half_life_days)
        s[0] = s[0] * f + (0.0 if e.accepted else 1.0)
        s[1] = s[1] * f + 1.0
        s[2] = e.timestamp

    def extend(self, events: Iterable[OutcomeEvent]) -> None:
        for e in events:
            self.append(e)

    def _fast(self, hospital_id: str, now: datetime) -> bool:
        s = self._sums.get(hospital_id)
        return s is not None and hospital_id not in self._exact and now >= s[2]

    def sources(self, hospital_id: str, until: datetime) -> set[Provenance]:
        """Provenance tags of this hospital's events up to `until` (O(1) on the fast path)."""
        if self._fast(hospital_id, until):
            return self._provs[hospital_id]
        return {e.source for e in self._by_h.get(hospital_id, []) if e.timestamp <= until}

    def counts(self, hospital_id: str, now: datetime) -> tuple[float, float]:
        s = self._sums.get(hospital_id)
        if s is None:
            return 0.0, 0.0
        if hospital_id in self._exact or now < s[2]:
            return decayed_counts(self._by_h[hospital_id], hospital_id, now, self.half_life_days)
        f = decay_weight((now - s[2]).total_seconds() / 86400.0, self.half_life_days)
        return s[0] * f, s[1] * f


def p_cannot_treat(
    capable: bool, rejects: float, total: float, prior_rejects: float, prior_total: float
) -> float:
    """1.0 if the capability is missing; otherwise the smoothed reject rate."""
    if not capable:
        return 1.0
    if prior_total <= 0 or not 0 <= prior_rejects <= prior_total:
        raise ValueError("need 0 <= prior_rejects <= prior_total and prior_total > 0")
    return (rejects + prior_rejects) / (total + prior_total)
