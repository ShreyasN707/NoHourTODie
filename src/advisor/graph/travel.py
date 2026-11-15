"""Travel-time layer of the Destination Advisor (spec §6).

Everything here answers one question: "how many minutes from A to B for an ambulance leaving at
time t?" through the `TravelTimeProvider` protocol (advisor.interfaces).

Building blocks
---------------
* `time_dependent_minutes`  free-flow seconds + departure time -> minutes, using the speed expected
  in each hour the ambulance is actually on the road (not the speed at departure).
* `apply_quantile`          expected minutes -> pessimistic (p80) minutes for critical patients.

Providers
---------
* `HaversineProfileTravelTime`  straight line x detour factor. Works with no data. PLACEHOLDER.
* `MatrixTravelTime`            explicit (origin, dest, hour) -> minutes table. Caller supplies the tag.
* `OsmGraphTravelTime`          GoldenHour's OSM road graph + Dijkstra (see advisor.graph.osm). PLACEHOLDER.

Provenance: nothing in this module is fitted yet. All speed/congestion numbers come from
config/advisor.yaml `travel.*` and are PLACEHOLDER, so every value returned here is tagged PLACEHOLDER
unless the caller of `MatrixTravelTime` supplies a stronger tag for its own table.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from advisor.config import AdvisorConfig, default_config
from advisor.models import LatLon
from advisor.provenance import Provenance, Sourced, Tagged

HOURS_PER_DAY = 24
SECONDS_PER_MINUTE = 60.0
SECONDS_PER_HOUR = 3600.0
# Mean Earth radius (IUGG). A physical constant, not a tunable, so it does not live in config.
EARTH_RADIUS_KM = 6371.0088


# ---------------------------------------------------------------- time-dependent conversion


def time_dependent_minutes(
    free_flow_s: float,
    depart: datetime,
    hourly_congestion: Sequence[float],
    ambulance_speed_multiplier: float,
) -> float:
    """Convert a free-flow travel time into minutes for an ambulance leaving at `depart`.

    `hourly_congestion[h]` is how many times longer a car takes than at free flow during hour h
    (0-23, local clock of `depart`). The ambulance beats car traffic by `ambulance_speed_multiplier`,
    so during hour h one second of free-flow driving costs  congestion[h] / ambulance_speed_multiplier
    real seconds.

    We walk forward through the clock: spend the rest of the current hour making progress at that
    hour's rate, then move to the next hour (wrapping past midnight via datetime arithmetic), until
    all free-flow seconds are used up. So a 17:50 departure pays 17:00 congestion for ten minutes
    and 18:00 congestion afterwards: the speed expected when the ambulance reaches each part of the
    route, not the speed now (spec §6).

    Why this is exact here: the multiplier is the same for every road segment in a given hour, so
    the order in which segments are driven does not matter; only the total free-flow work does.
    Limitation: once multipliers differ by road class or corridor, this shortcut no longer holds and
    the route itself can change with time; that needs a true time-dependent Dijkstra.
    """
    if len(hourly_congestion) != HOURS_PER_DAY:
        raise ValueError(f"hourly_congestion needs {HOURS_PER_DAY} values, got {len(hourly_congestion)}")
    if any(c <= 0 for c in hourly_congestion):
        raise ValueError("hourly_congestion multipliers must be positive")
    if ambulance_speed_multiplier <= 0:
        raise ValueError("ambulance_speed_multiplier must be positive")
    if free_flow_s < 0 or not math.isfinite(free_flow_s):
        raise ValueError(f"free_flow_s must be finite and non-negative, got {free_flow_s}")

    remaining_free_s = free_flow_s  # free-flow driving still to do
    elapsed_s = 0.0  # real clock time spent so far
    t = depart
    while remaining_free_s > 0:
        slowdown = hourly_congestion[t.hour] / ambulance_speed_multiplier  # real s per free-flow s
        next_hour = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        window_s = (next_hour - t).total_seconds()
        coverable_free_s = window_s / slowdown
        if remaining_free_s <= coverable_free_s:
            elapsed_s += remaining_free_s * slowdown
            break
        remaining_free_s -= coverable_free_s
        elapsed_s += window_s
        t = next_hour
    return elapsed_s / SECONDS_PER_MINUTE


def profile_minutes(free_flow_s: float, depart: datetime, cfg: AdvisorConfig) -> float:
    """`time_dependent_minutes` with the hourly profile and ambulance multiplier from config."""
    return time_dependent_minutes(
        free_flow_s,
        depart,
        cfg.v("travel.hourly_congestion"),
        float(cfg.v("travel.ambulance_speed_multiplier")),
    )


# ---------------------------------------------------------------- quantiles


def apply_quantile(expected: Sourced[float], quantile: float | None, cfg: AdvisorConfig) -> Sourced[float]:
    """Expected minutes -> minutes at the requested quantile.

    quantile=None returns `expected` unchanged. The only other quantile we model is the configured
    pessimistic one (scoring.pessimistic_travel_quantile, 0.8): we multiply the expected time by
    travel.pessimistic_multiplier_p80. That ratio is a PLACEHOLDER (not fitted from travel-time
    spread), so the result is always tagged PLACEHOLDER whatever the input's tag was. Any other
    quantile raises ValueError rather than silently returning a number we have no basis for.
    """
    if quantile is None:
        return expected
    supported = float(cfg.v("scoring.pessimistic_travel_quantile"))
    if not math.isclose(quantile, supported):
        raise ValueError(
            f"only quantile=None (expected) or {supported} (pessimistic) is modelled, got {quantile}"
        )
    mult = float(cfg.v("travel.pessimistic_multiplier_p80"))
    return Sourced[float](
        value=expected.value * mult,
        tag=Tagged(
            provenance=Provenance.PLACEHOLDER,
            source=(
                f"p{round(supported * 100)} = expected x travel.pessimistic_multiplier_p80 ({mult}, "
                f"PLACEHOLDER); expected from: {expected.tag.source}"
            ),
        ),
    )


# ---------------------------------------------------------------- haversine fallback


def haversine_km(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


class HaversineProfileTravelTime:
    """Fallback provider that needs no data files.

    road km   = straight-line km x travel.detour_factor
    free-flow = road km / travel.fallback_free_flow_kmh
    minutes   = free-flow pushed through the hourly congestion profile (`time_dependent_minutes`).

    Provenance: PLACEHOLDER. Detour factor, speed, congestion profile and ambulance multiplier are
    all unfitted config values. Use only when the OSM graph has not been built.
    """

    def __init__(self, cfg: AdvisorConfig | None = None) -> None:
        self.cfg = cfg or default_config()

    def free_flow_seconds(self, origin: LatLon, dest: LatLon) -> float:
        road_km = haversine_km(origin, dest) * float(self.cfg.v("travel.detour_factor"))
        return road_km / float(self.cfg.v("travel.fallback_free_flow_kmh")) * SECONDS_PER_HOUR

    def minutes(
        self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None
    ) -> Sourced[float]:
        expected = Sourced[float](
            value=profile_minutes(self.free_flow_seconds(origin, dest), depart, self.cfg),
            tag=Tagged(
                provenance=Provenance.PLACEHOLDER,
                source=(
                    "haversine x travel.detour_factor at travel.fallback_free_flow_kmh, scaled hour by hour "
                    "by travel.hourly_congestion / travel.ambulance_speed_multiplier (all PLACEHOLDER)"
                ),
            ),
        )
        return apply_quantile(expected, quantile, self.cfg)


# ---------------------------------------------------------------- explicit table


class MatrixTravelTime:
    """Provider backed by an explicit table of minutes per (origin_id, dest_id, departure hour).

    `points` names the locations (scene, base, hospitals); a query must use exactly one of those
    coordinates, otherwise KeyError (we never guess a nearby entry). The table value for hour h is
    the expected trip time for a departure during hour h, e.g. a sampled routing-API time; no
    interpolation between hours and no hour-walking, because the source already measured whole trips.

    Provenance: whatever `tag` the caller supplies for the table (e.g. SIMULATED for the spec's worked
    example, REAL_DERIVED once filled from sampled routing-API times). Pessimistic quantiles go
    through `apply_quantile` and are therefore PLACEHOLDER.
    """

    def __init__(
        self,
        points: Mapping[str, LatLon],
        table: Mapping[tuple[str, str, int], float],
        tag: Tagged,
        cfg: AdvisorConfig | None = None,
    ) -> None:
        bad_hours = sorted({h for (_, _, h) in table if not 0 <= h < HOURS_PER_DAY})
        if bad_hours:
            raise ValueError(f"table hours must be 0-23, got {bad_hours}")
        unknown = sorted({k for (o, d, _) in table for k in (o, d) if k not in points})
        if unknown:
            raise ValueError(f"table refers to unnamed points: {unknown}")
        self._ids = {(p.lat, p.lon): pid for pid, p in points.items()}
        if len(self._ids) != len(points):
            raise ValueError("two point ids share the same coordinates")
        self.table = dict(table)
        self.tag = tag
        self.cfg = cfg or default_config()

    def _id(self, p: LatLon) -> str:
        try:
            return self._ids[(p.lat, p.lon)]
        except KeyError:
            raise KeyError(f"no point registered at ({p.lat}, {p.lon})") from None

    def minutes(
        self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None
    ) -> Sourced[float]:
        key = (self._id(origin), self._id(dest), depart.hour)
        if key not in self.table:
            raise KeyError(f"no travel time for {key[0]} -> {key[1]} at hour {key[2]}")
        expected = Sourced[float](
            value=float(self.table[key]),
            tag=self.tag.model_copy(
                update={"source": f"{self.tag.source} [{key[0]} -> {key[1]}, hour {key[2]}]"}
            ),
        )
        return apply_quantile(expected, quantile, self.cfg)
