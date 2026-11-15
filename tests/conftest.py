"""Shared test helpers. All hospitals here are FICTIONAL and tagged SIMULATED ("test fixture")."""

from __future__ import annotations

from datetime import datetime

import pytest

from advisor.config import AdvisorConfig, load_config
from advisor.models import Assessment, Capability, Hospital, LatLon
from advisor.provenance import Provenance, Sourced, Tagged

FIXTURE = Tagged(provenance=Provenance.SIMULATED, source="test fixture")
T11 = datetime(2026, 11, 16, 11, 0)
T18 = datetime(2026, 11, 16, 18, 0)


@pytest.fixture
def cfg() -> AdvisorConfig:
    return load_config()


def hospital(hid: str, lat: float, caps: list[str], lon: float = 77.6) -> Hospital:
    return Hospital(
        hospital_id=hid, name=f"Hospital {hid} (fictional)", lat=lat, lon=lon, ownership="govt",
        location_tag=FIXTURE,
        capabilities={c: Capability(available=True, hours="24x7", **FIXTURE.model_dump()) for c in caps},
    )


class FakeTravel:
    """Travel minutes looked up by destination latitude (and hour). Ignores quantile on purpose,
    so tests can use the spec's worked-example numbers exactly."""

    def __init__(self, by_lat: dict[float, float | dict[int, float]], back: float = 10.0):
        self.by_lat, self.back = by_lat, back
        self.calls: list[tuple[LatLon, LatLon, float | None]] = []

    def minutes(self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None):
        self.calls.append((origin, dest, quantile))
        v = self.by_lat.get(dest.lat, self.back)
        if isinstance(v, dict):
            v = v[depart.hour]
        return Sourced(value=float(v), tag=Tagged(provenance=Provenance.PLACEHOLDER, source="fake travel"))


def assessment(complaint: str, when: datetime = T11, **kw) -> Assessment:
    return Assessment(case_id="C1", scene_lat=12.9, scene_lon=77.6, base_lat=12.95, base_lon=77.6,
                      complaint=complaint, when=when, **kw)
