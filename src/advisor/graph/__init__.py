"""Travel-time providers. `default_travel_provider()` picks the best one available on this machine."""

from __future__ import annotations

from advisor.config import AdvisorConfig, default_config
from advisor.interfaces import TravelTimeProvider


def default_travel_provider(cfg: AdvisorConfig | None = None) -> TravelTimeProvider:
    """OSM road graph if GoldenHour's data/processed graph files exist, else the haversine fallback.

    Both return PLACEHOLDER-tagged times (road network is real OSM; speeds are estimates).
    """
    from advisor.graph.osm import OsmGraphTravelTime
    from advisor.graph.travel import HaversineProfileTravelTime

    cfg = cfg or default_config()
    if OsmGraphTravelTime.available():
        return OsmGraphTravelTime(cfg=cfg)
    return HaversineProfileTravelTime(cfg=cfg)
