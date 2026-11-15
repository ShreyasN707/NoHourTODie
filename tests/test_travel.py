"""Tests for the travel-time layer (advisor.graph.travel and advisor.graph.osm)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from advisor.config import default_config
from advisor.graph.osm import OsmGraphTravelTime, RoadGraphMissingError
from advisor.graph.travel import (
    HaversineProfileTravelTime,
    MatrixTravelTime,
    apply_quantile,
    haversine_km,
    profile_minutes,
    time_dependent_minutes,
)
from advisor.interfaces import TravelTimeProvider
from advisor.models import LatLon
from advisor.provenance import Provenance, Sourced, Tagged

CFG = default_config()
P80 = CFG.v("scoring.pessimistic_travel_quantile")
P80_MULT = CFG.v("travel.pessimistic_multiplier_p80")


def profile(**by_hour: float) -> list[float]:
    """24-hour profile of 1.0 with chosen hours overridden, e.g. profile(h17=1.0, h18=2.0)."""
    p = [1.0] * 24
    for k, v in by_hour.items():
        p[int(k[1:])] = v
    return p


# ---------------------------------------------------------------- time-dependent helper


def test_constant_profile_is_simple_scaling():
    mins = time_dependent_minutes(600, datetime(2026, 11, 15, 3, 0), [2.0] * 24, 1.25)
    assert mins == pytest.approx(600 * 2.0 / 1.25 / 60)


def test_trip_inside_one_hour_uses_that_hour():
    mins = time_dependent_minutes(600, datetime(2026, 11, 15, 11, 0), profile(h11=2.0), 1.0)
    assert mins == pytest.approx(20.0)


def test_speed_changes_when_ambulance_reaches_next_hour():
    # 17:50 departure, 20 free-flow minutes. 17:00 is free flow (1x), 18:00 is 2x.
    # 10 real minutes cover 10 free-flow minutes before 18:00; remaining 10 take 20 -> 30 total.
    mins = time_dependent_minutes(1200, datetime(2026, 11, 15, 17, 50), profile(h17=1.0, h18=2.0), 1.0)
    assert mins == pytest.approx(30.0)
    # Using only the departure-hour speed would wrongly give 20.
    assert mins != pytest.approx(20.0)


def test_trip_crosses_midnight():
    # 23:55 departure: 5 min at 1x, then remaining 5 free-flow min at 3x (hour 0) -> 20 min.
    mins = time_dependent_minutes(600, datetime(2026, 11, 15, 23, 55), profile(h23=1.0, h0=3.0), 1.0)
    assert mins == pytest.approx(20.0)


def test_long_trip_spans_several_hours():
    # 3 h free flow from 00:00 with 2x everywhere -> 6 h.
    mins = time_dependent_minutes(3 * 3600, datetime(2026, 11, 15, 0, 0), [2.0] * 24, 1.0)
    assert mins == pytest.approx(360.0)


def test_zero_distance_is_zero_minutes():
    assert time_dependent_minutes(0, datetime(2026, 11, 15, 9, 0), [2.0] * 24, 1.0) == 0.0


@pytest.mark.parametrize(
    "free_s, congestion, amb",
    [(-1, [1.0] * 24, 1.0), (float("inf"), [1.0] * 24, 1.0), (60, [1.0] * 23, 1.0),
     (60, [0.0] * 24, 1.0), (60, [1.0] * 24, 0.0)],
)
def test_helper_rejects_bad_input(free_s, congestion, amb):
    with pytest.raises(ValueError):
        time_dependent_minutes(free_s, datetime(2026, 11, 15, 9, 0), congestion, amb)


def test_config_profile_peak_slower_than_night():
    night = profile_minutes(900, datetime(2026, 11, 15, 3, 0), CFG)
    peak = profile_minutes(900, datetime(2026, 11, 15, 18, 0), CFG)
    assert peak > night


# ---------------------------------------------------------------- quantile


def test_quantile_none_passes_through_and_p80_scales_and_is_placeholder():
    expected = Sourced[float](
        value=10.0, tag=Tagged(provenance=Provenance.REAL_DERIVED, source="fitted", as_of=date(2026, 11, 1))
    )
    assert apply_quantile(expected, None, CFG) is expected
    p80 = apply_quantile(expected, P80, CFG)
    assert p80.value == pytest.approx(10.0 * P80_MULT)
    assert p80.provenance is Provenance.PLACEHOLDER
    assert "fitted" in p80.tag.source


def test_unmodelled_quantile_is_refused():
    expected = Sourced[float](value=10.0, tag=Tagged(provenance=Provenance.PLACEHOLDER, source="x"))
    with pytest.raises(ValueError):
        apply_quantile(expected, 0.5, CFG)


# ---------------------------------------------------------------- haversine fallback

MAJESTIC = LatLon(lat=12.9767, lon=77.5713)
VICTORIA = LatLon(lat=12.9634, lon=77.5736)


def test_haversine_known_distance():
    # One degree of latitude is ~111.2 km.
    assert haversine_km(LatLon(lat=12.0, lon=77.0), LatLon(lat=13.0, lon=77.0)) == pytest.approx(111.2, abs=0.1)


def test_haversine_provider_matches_formula_and_is_placeholder():
    prov = HaversineProfileTravelTime(CFG)
    assert isinstance(prov, TravelTimeProvider)
    depart = datetime(2026, 11, 15, 3, 0)
    free_s = (
        haversine_km(MAJESTIC, VICTORIA) * CFG.v("travel.detour_factor")
        / CFG.v("travel.fallback_free_flow_kmh") * 3600
    )
    out = prov.minutes(MAJESTIC, VICTORIA, depart)
    assert out.value == pytest.approx(profile_minutes(free_s, depart, CFG))
    assert out.provenance is Provenance.PLACEHOLDER
    assert "haversine" in out.tag.source

    pess = prov.minutes(MAJESTIC, VICTORIA, depart, quantile=P80)
    assert pess.value == pytest.approx(out.value * P80_MULT)
    assert pess.provenance is Provenance.PLACEHOLDER


def test_haversine_provider_rush_hour_is_slower():
    prov = HaversineProfileTravelTime(CFG)
    night = prov.minutes(MAJESTIC, VICTORIA, datetime(2026, 11, 15, 3, 0)).value
    rush = prov.minutes(MAJESTIC, VICTORIA, datetime(2026, 11, 15, 18, 0)).value
    assert rush > night


# ---------------------------------------------------------------- matrix provider + traffic flip

SCENE = LatLon(lat=12.97, lon=77.59)
NEAR = LatLon(lat=12.98, lon=77.60)
FAR = LatLon(lat=13.02, lon=77.65)
WORKED_EXAMPLE = Tagged(provenance=Provenance.SIMULATED, source="spec worked example: traffic flip")


def flip_matrix() -> MatrixTravelTime:
    return MatrixTravelTime(
        points={"scene": SCENE, "near": NEAR, "far": FAR},
        table={
            ("scene", "near", 11): 10.0, ("scene", "near", 18): 28.0,
            ("scene", "far", 11): 20.0, ("scene", "far", 18): 24.0,
        },
        tag=WORKED_EXAMPLE,
        cfg=CFG,
    )


def test_traffic_flip_changes_ranking():
    m = flip_matrix()
    assert isinstance(m, TravelTimeProvider)
    hospitals = {"near": NEAR, "far": FAR}

    def fastest(depart: datetime) -> str:
        return min(hospitals, key=lambda h: m.minutes(SCENE, hospitals[h], depart).value)

    assert fastest(datetime(2026, 11, 15, 11, 0)) == "near"  # 10 vs 20
    assert fastest(datetime(2026, 11, 15, 18, 0)) == "far"  # 28 vs 24


def test_matrix_keeps_caller_tag_and_p80_is_placeholder():
    m = flip_matrix()
    out = m.minutes(SCENE, NEAR, datetime(2026, 11, 15, 18, 30))
    assert out.value == 28.0
    assert out.provenance is Provenance.SIMULATED
    assert "scene -> near, hour 18" in out.tag.source
    pess = m.minutes(SCENE, NEAR, datetime(2026, 11, 15, 18, 30), quantile=P80)
    assert pess.value == pytest.approx(28.0 * P80_MULT)
    assert pess.provenance is Provenance.PLACEHOLDER


def test_matrix_refuses_to_guess():
    m = flip_matrix()
    with pytest.raises(KeyError):
        m.minutes(SCENE, LatLon(lat=12.99, lon=77.61), datetime(2026, 11, 15, 11, 0))
    with pytest.raises(KeyError):
        m.minutes(SCENE, NEAR, datetime(2026, 11, 15, 9, 0))


def test_matrix_validates_table():
    with pytest.raises(ValueError):
        MatrixTravelTime({"a": SCENE}, {("a", "b", 11): 5.0}, WORKED_EXAMPLE, CFG)
    with pytest.raises(ValueError):
        MatrixTravelTime({"a": SCENE, "b": NEAR}, {("a", "b", 24): 5.0}, WORKED_EXAMPLE, CFG)


# ---------------------------------------------------------------- OSM graph provider


def write_graph(d: Path) -> None:
    """Tiny directed graph, GoldenHour's schema.

        0 --60s--> 1 --60s--> 2
        0 ------------300s--> 2      (direct but slower)
        2 --60s--> 0                 (way back)
        3 (isolated: reachable from nobody)
    """
    nodes = pd.DataFrame({
        "node_idx": [0, 1, 2, 3],
        "osm_id": [100, 101, 102, 103],
        "lat": [12.970, 12.970, 12.970, 13.100],
        "lon": [77.590, 77.600, 77.610, 77.700],
    })
    edges = pd.DataFrame({
        "u": [0, 1, 0, 2],
        "v": [1, 2, 2, 0],
        "time_s": [60.0, 60.0, 300.0, 60.0],
    })
    nodes.to_parquet(d / "graph_nodes.parquet", index=False)
    edges.to_parquet(d / "graph_edges.parquet", index=False)


def test_osm_missing_files_gives_clear_error(tmp_path):
    assert not OsmGraphTravelTime.available(tmp_path)
    with pytest.raises(RoadGraphMissingError, match="make geo") as exc:
        OsmGraphTravelTime(tmp_path, CFG)
    assert "graph_nodes.parquet" in str(exc.value)


def test_osm_shortest_path_through_profile(tmp_path):
    write_graph(tmp_path)
    assert OsmGraphTravelTime.available(tmp_path)
    prov = OsmGraphTravelTime(tmp_path, CFG)
    assert isinstance(prov, TravelTimeProvider)

    origin = LatLon(lat=12.9701, lon=77.5901)  # snaps to node 0
    dest = LatLon(lat=12.9699, lon=77.6099)  # snaps to node 2
    depart = datetime(2026, 11, 15, 18, 0)
    out = prov.minutes(origin, dest, depart)
    assert out.value == pytest.approx(profile_minutes(120.0, depart, CFG))  # via node 1, not direct 300 s
    assert out.provenance is Provenance.PLACEHOLDER
    assert "snapped origin" in out.tag.source

    back = prov.minutes(dest, origin, depart)
    assert back.value == pytest.approx(profile_minutes(60.0, depart, CFG))

    pess = prov.minutes(origin, dest, depart, quantile=P80)
    assert pess.value == pytest.approx(out.value * P80_MULT)


def test_osm_caches_per_source(tmp_path):
    write_graph(tmp_path)
    prov = OsmGraphTravelTime(tmp_path, CFG)
    origin = LatLon(lat=12.970, lon=77.590)
    for lon in (77.600, 77.610):
        prov.minutes(origin, LatLon(lat=12.970, lon=lon), datetime(2026, 11, 15, 9, 0))
    assert list(prov._cache) == [0]


def test_osm_unreachable_raises(tmp_path):
    write_graph(tmp_path)
    prov = OsmGraphTravelTime(tmp_path, CFG)
    with pytest.raises(ValueError, match="unreachable"):
        prov.minutes(LatLon(lat=12.970, lon=77.590), LatLon(lat=13.100, lon=77.700), datetime(2026, 11, 15, 9))
