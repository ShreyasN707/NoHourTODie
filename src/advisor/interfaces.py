"""Contracts between advisor subpackages. Implementations live in graph/ and passive/.

Change these only here. The rules engine depends on nothing but these protocols, so it can be
tested with fakes and the real road graph / 108 logs can be swapped in without touching it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from advisor.models import LatLon
from advisor.provenance import Sourced


@runtime_checkable
class TravelTimeProvider(Protocol):
    def minutes(
        self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None
    ) -> Sourced[float]:
        """Ambulance travel time in minutes leaving `origin` at `depart`.

        Time-dependent: speeds are those expected when the ambulance reaches each segment (spec §6).
        quantile=None -> expected (mean) time; quantile=0.8 -> pessimistic time for critical patients.
        The returned tag says REAL_DERIVED (fitted profile) or PLACEHOLDER (fallback multipliers).
        """
        ...


@runtime_checkable
class DwellIndex(Protocol):
    def mean_offload_min(self, hospital_id: str, when: datetime) -> Sourced[float] | None:
        """Rolling mean ambulance gate-dwell (offload proxy) at this hospital for the hour band of `when`.

        None when there is not enough data for that hospital/hour band; the caller then falls back
        to the ED-status lookup. Tag is REAL only when built from the 108 GPS logs; SIMULATED otherwise.
        """
        ...
