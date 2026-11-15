"""Road-graph travel times on GoldenHour's OSM graph (module A output, reused by module B).

GoldenHour's `make geo` (pipeline/geo.py) parses OpenStreetMap roads, keeps the largest strongly
connected component and writes two tables to data/processed/:

  graph_nodes.parquet  node_idx, osm_id, lat, lon
  graph_edges.parquet  u, v, time_s   (directed; free-flow car seconds at ROAD_SPEEDS_KMH per road class)

One graph covers the whole study bounding box (Bengaluru "blr" and Tumakuru "tmk"); it is not split
per region, so the same files serve Bengaluru.

This module needs scipy (sparse graphs, Dijkstra, KD-tree) and pyarrow (parquet). Both come with
GoldenHour's environment.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
import scipy.sparse.csgraph as csg
from scipy.spatial import cKDTree

from advisor.config import ROOT_DIR, AdvisorConfig, default_config
from advisor.graph.travel import EARTH_RADIUS_KM, apply_quantile, profile_minutes
from advisor.models import LatLon
from advisor.provenance import Provenance, Sourced, Tagged

DEFAULT_PROCESSED_DIR = ROOT_DIR / "data" / "processed"
NODES_FILE = "graph_nodes.parquet"
EDGES_FILE = "graph_edges.parquet"


class RoadGraphMissingError(FileNotFoundError):
    """GoldenHour's processed road graph is not on disk."""


class OsmGraphTravelTime:
    """Shortest free-flow path on the OSM road graph, then the hourly congestion profile.

    1. Snap origin and destination to the nearest graph node (KD-tree on lat / lon x cos(lat), a local
       flat-earth projection; fine at city scale).
    2. Single-source Dijkstra from the origin node over free-flow edge seconds. The full distance
       array is cached per origin node, so ranking N hospitals from one scene costs one Dijkstra.
    3. Free-flow seconds -> minutes with `time_dependent_minutes` (speed expected in each hour the
       ambulance is on the road).

    The path is chosen on free-flow times. Because the congestion multiplier is uniform across roads in
    a given hour, the free-flow shortest path is also the fastest path at any departure time.

    Not counted: the walk/drive between the exact point and its snapped node. The snap distances are
    written into the `source` string so they stay visible.

    Provenance: PLACEHOLDER. Edge speeds are GoldenHour's per-road-class guesses and the hourly
    multipliers are hand-shaped, not fitted. It becomes REAL_DERIVED (with an as_of date) once the
    multipliers are fitted from a week of sampled routing-API times (spec §6), and ambulance-specific
    once 108 GPS logs arrive.

    `max_cached_sources` bounds memory (each cached origin holds one float per graph node); it does not
    change any result.
    """

    def __init__(
        self,
        processed_dir: Path | str = DEFAULT_PROCESSED_DIR,
        cfg: AdvisorConfig | None = None,
        max_cached_sources: int = 64,
    ) -> None:
        self.cfg = cfg or default_config()
        self.processed_dir = Path(processed_dir)
        nodes_path, edges_path = self.graph_files(self.processed_dir)
        missing = [str(p) for p in (nodes_path, edges_path) if not p.exists()]
        if missing:
            raise RoadGraphMissingError(
                f"road graph not found: {', '.join(missing)}. Run GoldenHour's `make geo` "
                "(pipeline/geo.py) first, or use HaversineProfileTravelTime as a fallback."
            )

        nodes = pd.read_parquet(nodes_path).sort_values("node_idx")
        edges = pd.read_parquet(edges_path)
        n = len(nodes)
        if not np.array_equal(nodes["node_idx"].to_numpy(), np.arange(n)):
            raise ValueError(f"{nodes_path}: node_idx must be 0..N-1 without gaps")

        self._lat = nodes["lat"].to_numpy(dtype=np.float64)
        self._lon = nodes["lon"].to_numpy(dtype=np.float64)
        self._cos_lat = math.cos(math.radians(float(self._lat.mean())))
        self._tree = cKDTree(np.column_stack([self._lat, self._lon * self._cos_lat]))
        self._adj = sp.csr_matrix(
            (
                edges["time_s"].to_numpy(dtype=np.float64),
                (edges["u"].to_numpy(dtype=np.int64), edges["v"].to_numpy(dtype=np.int64)),
            ),
            shape=(n, n),
        )
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._max_cached = max_cached_sources

    @staticmethod
    def graph_files(processed_dir: Path | str = DEFAULT_PROCESSED_DIR) -> tuple[Path, Path]:
        d = Path(processed_dir)
        return d / NODES_FILE, d / EDGES_FILE

    @classmethod
    def available(cls, processed_dir: Path | str = DEFAULT_PROCESSED_DIR) -> bool:
        """True when both graph tables exist, so callers can pick this provider or the fallback."""
        return all(p.exists() for p in cls.graph_files(processed_dir))

    def snap(self, p: LatLon) -> tuple[int, float]:
        """Nearest graph node and its approximate distance in metres."""
        dist_deg, idx = self._tree.query([p.lat, p.lon * self._cos_lat])
        return int(idx), float(math.radians(dist_deg) * EARTH_RADIUS_KM * 1000.0)

    def _distances_from(self, source: int) -> np.ndarray:
        if source in self._cache:
            self._cache.move_to_end(source)
            return self._cache[source]
        dist = csg.dijkstra(self._adj, directed=True, indices=source)
        self._cache[source] = dist
        if len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)
        return dist

    def free_flow_seconds(self, origin: LatLon, dest: LatLon) -> tuple[float, float, float]:
        """(free-flow seconds, origin snap metres, dest snap metres)."""
        o, o_snap_m = self.snap(origin)
        d, d_snap_m = self.snap(dest)
        seconds = float(self._distances_from(o)[d])
        if not math.isfinite(seconds):
            raise ValueError(f"graph node {d} is unreachable from node {o}")
        return seconds, o_snap_m, d_snap_m

    def minutes(
        self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None
    ) -> Sourced[float]:
        free_s, o_snap_m, d_snap_m = self.free_flow_seconds(origin, dest)
        expected = Sourced[float](
            value=profile_minutes(free_s, depart, self.cfg),
            tag=Tagged(
                provenance=Provenance.PLACEHOLDER,
                source=(
                    "OSM road graph (GoldenHour pipeline/geo.py, per-road-class free-flow speeds) shortest "
                    "path, scaled hour by hour by travel.hourly_congestion / travel.ambulance_speed_multiplier "
                    f"(all PLACEHOLDER); snapped origin {o_snap_m:.0f} m, destination {d_snap_m:.0f} m"
                ),
            ),
        )
        return apply_quantile(expected, quantile, self.cfg)
