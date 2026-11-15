"""Precomputed road travel times for the replay, wrapped as a `TravelTimeProvider`.

Running one Dijkstra per incident on the 1.7M-node OSM graph would take hours, so we run a few per hospital:
  * reverse Dijkstra from each hospital (on the transposed graph): seconds from EVERY node to that hospital.
    We keep the columns for scene cells and hospitals (scene -> hospital, hospital -> hospital).
  * forward Dijkstra from each hospital: seconds from that hospital to standby sites (return to base)
    and to other hospitals.
That is 2 x (number of hospitals) Dijkstras, a few seconds each batch, cached in data/processed/ (gitignored),
keyed by a hash of the hospital set, the cell and site lists and the graph files.

`SimTravel` turns those free-flow seconds into minutes exactly like `OsmGraphTravelTime` does
(same snapping, `profile_minutes`, `apply_quantile`), so the rules engine runs unchanged and gets the
same numbers the API would. Provenance: PLACEHOLDER (road-class speeds and hourly congestion are
unfitted), road network itself REAL (OpenStreetMap).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from advisor.config import AdvisorConfig
from advisor.graph.osm import DEFAULT_PROCESSED_DIR, OsmGraphTravelTime
from advisor.graph.travel import apply_quantile, profile_minutes
from advisor.models import LatLon
from advisor.provenance import Provenance, Sourced, Tagged

Point = tuple[float, float]  # (lat, lon)
CACHE_PREFIX = "advisor_sim_travel_"
TRAVEL_SOURCE = (
    "OSM road graph (GoldenHour pipeline/geo.py, per-road-class free-flow speeds), shortest paths "
    "precomputed per hospital for the replay, scaled hour by hour by travel.hourly_congestion / "
    "travel.ambulance_speed_multiplier (all PLACEHOLDER)"
)


@dataclass
class TravelTables:
    """Free-flow seconds between the points the replay uses, indexed by snapped graph node.

    to_hosp_s[h, j]:   seconds from origin_nodes[j] to hospital h
    from_hosp_s[h, k]: seconds from hospital h to dest_nodes[k]
    point_nodes:       snapped node for every known point (scene cells, hospitals, standby sites)
    """

    hosp_nodes: np.ndarray
    origin_nodes: np.ndarray
    to_hosp_s: np.ndarray
    dest_nodes: np.ndarray
    from_hosp_s: np.ndarray
    point_nodes: dict[Point, int]

    def save(self, path: Path) -> None:
        pts = list(self.point_nodes.items())
        np.savez_compressed(
            path, hosp_nodes=self.hosp_nodes, origin_nodes=self.origin_nodes, to_hosp_s=self.to_hosp_s,
            dest_nodes=self.dest_nodes, from_hosp_s=self.from_hosp_s,
            pt_lat=np.array([p[0][0] for p in pts]), pt_lon=np.array([p[0][1] for p in pts]),
            pt_node=np.array([p[1] for p in pts], dtype=np.int64),
        )

    @classmethod
    def load(cls, path: Path) -> TravelTables:
        z = np.load(path)
        pts = {(float(a), float(b)): int(n) for a, b, n in zip(z["pt_lat"], z["pt_lon"], z["pt_node"], strict=True)}
        return cls(hosp_nodes=z["hosp_nodes"], origin_nodes=z["origin_nodes"], to_hosp_s=z["to_hosp_s"],
                   dest_nodes=z["dest_nodes"], from_hosp_s=z["from_hosp_s"], point_nodes=pts)


# ---------------------------------------------------------------- precompute


def compute_tables(adj, node_lat: np.ndarray, node_lon: np.ndarray, hospitals: Sequence[Point],
                   origins: Sequence[Point], dests: Sequence[Point]) -> TravelTables:
    """Snap every point to the graph and run 2 Dijkstras per hospital.

    adj: scipy CSR matrix of directed free-flow edge seconds. Hospitals are added to both origins
    and destinations so hospital -> hospital moves (secondary transfers) are covered.
    Snapping matches `OsmGraphTravelTime.snap` (lat, lon x cos(mean lat), nearest node).
    """
    import scipy.sparse.csgraph as csg
    from scipy.spatial import cKDTree

    cos_lat = math.cos(math.radians(float(node_lat.mean())))
    tree = cKDTree(np.column_stack([node_lat, node_lon * cos_lat]))
    all_pts = list(dict.fromkeys([*hospitals, *origins, *dests]))
    _, idx = tree.query(np.array([[p[0], p[1] * cos_lat] for p in all_pts]))
    point_nodes = {p: int(i) for p, i in zip(all_pts, idx, strict=True)}

    hosp_nodes = np.array([point_nodes[p] for p in hospitals], dtype=np.int64)
    origin_nodes = np.array(sorted({point_nodes[p] for p in [*origins, *hospitals]}), dtype=np.int64)
    dest_nodes = np.array(sorted({point_nodes[p] for p in [*dests, *hospitals]}), dtype=np.int64)
    rev = adj.T.tocsr()
    to_hosp = np.empty((len(hospitals), len(origin_nodes)))
    from_hosp = np.empty((len(hospitals), len(dest_nodes)))
    for h, node in enumerate(hosp_nodes):
        to_hosp[h] = csg.dijkstra(rev, directed=True, indices=int(node))[origin_nodes]
        from_hosp[h] = csg.dijkstra(adj, directed=True, indices=int(node))[dest_nodes]
    if not (np.isfinite(to_hosp).all() and np.isfinite(from_hosp).all()):
        raise ValueError("some replay point is unreachable on the road graph")
    return TravelTables(hosp_nodes=hosp_nodes, origin_nodes=origin_nodes, to_hosp_s=to_hosp,
                        dest_nodes=dest_nodes, from_hosp_s=from_hosp, point_nodes=point_nodes)


def _cache_key(hospitals: Sequence[Point], origins: Sequence[Point], dests: Sequence[Point],
               processed_dir: Path) -> str:
    graph = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in OsmGraphTravelTime.graph_files(processed_dir)]
    blob = json.dumps([list(hospitals), list(origins), list(dests), graph], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_or_compute_tables(hospitals: Sequence[Point], origins: Sequence[Point], dests: Sequence[Point],
                           processed_dir: Path | str = DEFAULT_PROCESSED_DIR) -> TravelTables:
    """Tables for these points from the real OSM graph, cached on disk by a hash of the inputs."""
    import pandas as pd
    import scipy.sparse as sp

    processed_dir = Path(processed_dir)
    cache = processed_dir / f"{CACHE_PREFIX}{_cache_key(hospitals, origins, dests, processed_dir)}.npz"
    if cache.exists():
        return TravelTables.load(cache)
    nodes_path, edges_path = OsmGraphTravelTime.graph_files(processed_dir)
    nodes = pd.read_parquet(nodes_path).sort_values("node_idx")
    edges = pd.read_parquet(edges_path)
    n = len(nodes)
    adj = sp.csr_matrix((edges["time_s"].to_numpy(dtype=np.float64),
                         (edges["u"].to_numpy(dtype=np.int64), edges["v"].to_numpy(dtype=np.int64))), shape=(n, n))
    tables = compute_tables(adj, nodes["lat"].to_numpy(np.float64), nodes["lon"].to_numpy(np.float64),
                            hospitals, origins, dests)
    tables.save(cache)
    return TravelTables.load(cache)  # round-trip so a fresh run and a cached run use identical floats


# ---------------------------------------------------------------- provider


class SimTravel:
    """`TravelTimeProvider` over precomputed tables. Unknown points raise KeyError (we never guess).

    Supported legs: any known point -> hospital, and hospital -> any known point.
    `expected_min` is the same number as `minutes(...).value` without building provenance objects;
    the replay uses it for its own (non-advisor) decisions and for actual drive times.
    """

    def __init__(self, tables: TravelTables, cfg: AdvisorConfig):
        self.t = tables
        self.cfg = cfg
        self._o = {int(n): j for j, n in enumerate(tables.origin_nodes)}
        self._d = {int(n): k for k, n in enumerate(tables.dest_nodes)}
        self._h: dict[int, int] = {}
        for h, n in enumerate(tables.hosp_nodes):
            self._h.setdefault(int(n), h)  # two hospitals on one node have identical distances

    def node(self, p: Point) -> int:
        try:
            return self.t.point_nodes[p]
        except KeyError:
            raise KeyError(f"no precomputed travel for point {p}") from None

    def free_flow_s(self, origin: Point, dest: Point) -> float:
        o, d = self.node(origin), self.node(dest)
        if o == d:
            return 0.0
        if d in self._h and o in self._o:
            return float(self.t.to_hosp_s[self._h[d], self._o[o]])
        if o in self._h and d in self._d:
            return float(self.t.from_hosp_s[self._h[o], self._d[d]])
        raise KeyError(f"no precomputed travel from {origin} to {dest}")

    def expected_min(self, origin: Point, dest: Point, depart: datetime) -> float:
        return profile_minutes(self.free_flow_s(origin, dest), depart, self.cfg)

    def minutes(self, origin: LatLon, dest: LatLon, depart: datetime, quantile: float | None = None
                ) -> Sourced[float]:
        expected = Sourced[float](
            value=self.expected_min((origin.lat, origin.lon), (dest.lat, dest.lon), depart),
            tag=Tagged(provenance=Provenance.PLACEHOLDER, source=TRAVEL_SOURCE),
        )
        return apply_quantile(expected, quantile, self.cfg)
