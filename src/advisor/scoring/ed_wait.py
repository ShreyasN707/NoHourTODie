"""expected_ED_wait(h): which number we use, in order of trust (spec §4, §5 "passive signals first").

1. Rolling gate-dwell mean for this hospital and hour band (108 GPS logs; REAL once provided).
2. Fresh self-reported offload wait from the live tier (SIMULATED: no public feed exists).
3. Fresh self-reported ED status -> lookup green/amber/red (PLACEHOLDER minutes).
4. Nothing usable -> assume `scoring.ed_status_when_unknown` (PLACEHOLDER).

Passive data comes first because self-reported dashboards in Bengaluru went stale (spec §5).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from advisor.config import AdvisorConfig
from advisor.interfaces import DwellIndex
from advisor.models import EDStatus, HospitalStatus
from advisor.provenance import Provenance, Sourced, Tagged


def is_fresh(status: HospitalStatus | None, when: datetime, max_age_min: float) -> bool:
    return status is not None and timedelta(0) <= when - status.timestamp <= timedelta(minutes=max_age_min)


def expected_ed_wait(
    hospital_id: str,
    when: datetime,
    status: HospitalStatus | None,
    dwell: DwellIndex | None,
    cfg: AdvisorConfig,
) -> Sourced[float]:
    if dwell is not None:
        d = dwell.mean_offload_min(hospital_id, when)
        if d is not None:
            return d

    fresh = is_fresh(status, when, cfg.v("scoring.live_status_max_age_min"))
    if fresh and status.offload_wait_min is not None:
        return Sourced(
            value=float(status.offload_wait_min),
            tag=Tagged(provenance=status.provenance, source=f"live offload report: {status.source}",
                       as_of=status.as_of),
        )

    lookup = cfg.p("scoring.ed_wait_by_status_min")
    if fresh and status.ed_status is not None:
        return Sourced(
            value=float(lookup.value[status.ed_status.value]),
            tag=Tagged(provenance=Provenance.PLACEHOLDER,
                       source=f"ED status '{status.ed_status.value}' ({status.provenance.value}) -> lookup"),
        )

    assumed = EDStatus(cfg.v("scoring.ed_status_when_unknown"))
    return Sourced(
        value=float(lookup.value[assumed.value]),
        tag=Tagged(provenance=Provenance.PLACEHOLDER,
                   source=f"no fresh data; assumed ED status '{assumed.value}'"),
    )
