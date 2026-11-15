"""GoldenHour rural add-on: Sakleshpura taluk and the Western Ghats passes (Shiradi, Charmadi).

What is real and what is modelled
  REAL      roads, hospitals and villages (OpenStreetMap, local southern-zone PBF), population (WorldPop 1 km),
            taluk boundary (OSM admin_level 6).
  MODELLED  calls (Tumakuru's synthetic per-person rate, call mix and hourly pattern applied to Sakleshpura's
            population), fleet size, monsoon slow-downs, landslide spots and chances, hospital capabilities,
            mobile dead zones. Every such number below is marked ≈ (our estimate) or ◐ (news/report).

Rural difficulties this file turns into map data
  1. Ghat roads close after landslides          -> closure scenarios, re-routing, unit from the other side
  2. One road in, one road out                   -> standby plan that still covers when the ghat is shut
  3. Few hospitals, nearest can't treat          -> referral chain: stabilise at taluk hospital, then hub
  4. Hospital refuses (no ICU bed)               -> pre-alert before leaving, re-route before the trip
  5. Patchy mobile signal                        -> dead-zone stretches flagged on every route
  6. Estate roads not drivable / not on the map  -> meeting point on the nearest public road
  7. Monsoon rain and fog on ghats               -> slower ghat speeds in the monsoon scenario

Run:  .venv/bin/python -m pipeline.rural          (writes data/processed/*_skp.* and web/public/data/rural_skp.json)
      .venv/bin/python -m pipeline.rural --check  (self-check of the robust MILP on a toy instance)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import h3
import numpy as np
import osmium
import pandas as pd
import scipy.sparse as sp
import scipy.sparse.csgraph as csg
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, shape
from shapely.ops import unary_union

from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    RAW_DIR,
    ROAD_SPEEDS_KMH,
    WEB_DATA_DIR,
)
from pipeline.geo import haversine_m, sample_population

# ----------------------------------------------------------------------------- assumptions
BBOX = (74.8, 12.35, 76.25, 13.35)  # Sakleshpura, Hassan, both ghats, Mangaluru
TALUK = "Sakaleshapura taluk"        # ◐ OSM admin_level 6 name
H3_RES = 8                            # ~0.74 km2: fine enough to show the narrow ghat corridor
TARGET_MIN = 10.0                     # ◐ Cabinet, Sep 2026: 10 min rural response target
RURAL_FLAT_FACTOR = 1.1               # ◐ same flat highway factor as Tumakuru (pipeline/config.py)

# The ghat sections: road ref + a box around the climb. ◐ Shiradi = NH-75 Gundya-Donigal; Charmadi = NH-73.
GHATS = {
    "shiradi": {"name": "Shiradi Ghat (NH-75)", "ref": "NH75", "box": (75.54, 12.80, 75.74, 12.935)},
    "charmadi": {"name": "Charmadi Ghat (NH-73)", "ref": "NH73", "box": (75.36, 12.99, 75.53, 13.14)},
}
LANDSLIDE_RADIUS_M = 300              # ≈ road shut within 300 m of the slip
MONSOON_GHAT_SLOWDOWN = 1.6           # ≈ rain, fog and hairpins: ghat roads ~40% slower in Jun-Sep
MONSOON_GENERAL_SLOWDOWN = 1.15       # ≈ wet roads elsewhere
P_MONSOON = 4 / 12                    # ◐ IMD south-west monsoon season Jun-Sep
P_SHIRADI_SHUT_IN_MONSOON = 0.25      # ≈ closures reported in the 2024 and 2025 monsoons; share of monsoon time shut
CORRIDOR_BUFFER_DEG = 0.012           # ≈ ~1.3 km either side of the Shiradi road, down to Gundya

FLEET_TALUK = 3                       # ≈ Tumakuru has 1 ambulance per ~350 km2; Sakleshpura is ~1,050 km2
NEIGHBOUR_UNIT = ("Subrahmanya Government Hospital", 12.6778, 75.5999)  # ≈ a unit in the next taluk (Kadaba)
NO_SIGNAL_KM = 4.0                    # ≈ no mobile signal > 4 km from any village or town, and on ghat sections

# Hospitals used in the demo. Names and places are REAL (OSM). Tier and capabilities are ≈ until checked
# with the hospitals and the Hrudaya Jyothi spoke/hub list (◐ 86 spokes, 10 hubs; Sakleshpura not confirmed).
HOSPITALS = [
    # id, OSM name to match, short label, tier, caps
    ("crawford", "Crawford Government Hospital", "Crawford Govt Hospital, Sakleshpura", "taluk",
     ["24x7 casualty", "clot-busting drug (assumed spoke)", "delivery"]),
    ("hassan_dh", "District Hospital", "District Hospital, Hassan (HIMS)", "hub",
     ["ICU", "CT scan", "trauma surgery", "clot-busting drug"]),
    ("hassan_mani", "Mani Super Speciality Hospital", "Mani Super Speciality, Hassan", "hub",
     ["ICU", "CT scan", "trauma surgery", "cath lab (assumed)"]),
    ("belur", "Government Hospital, Belur", "Govt Hospital, Belur", "taluk", ["24x7 casualty"]),
    ("alur", "Alur Government Hospital", "Alur Govt Hospital", "taluk", ["24x7 casualty"]),
    ("subrahmanya", "Subrahmanya Government Hospital", "Subrahmanya Govt Hospital", "taluk", ["24x7 casualty"]),
    ("ujire", "SDM Hospital", "SDM Hospital, Ujire", "taluk", ["24x7 casualty", "ICU (assumed)"]),
    ("wenlock", "Wenlock", "Wenlock Govt Hospital, Mangaluru", "hub", ["ICU", "CT scan", "trauma surgery"]),
]

PLACES = ("city", "town", "village", "hamlet", "suburb")
SITE_TYPES = {"hospital": 0, "fire": 1, "police": 2, "clinic": 3, "fuel": 4, "town": 5, "village": 6}

OUT = PROCESSED_DIR
SKP_PBF = RAW_DIR / "skp.osm.pbf"
WEB_JSON = WEB_DATA_DIR / "rural_skp.json"


def in_box(lat, lon, box):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


# ----------------------------------------------------------------------------- OSM parsing
class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.coords: dict[int, tuple[float, float]] = {}
        self.edges: list[tuple[int, int, float, str]] = []  # u, v, free-flow s, ghat id or ""
        self.pois: list[tuple[str, str, float, float]] = []
        self.places: list[tuple[str, str, float, float]] = []
        self.tracks: list[tuple[str, list[tuple[float, float]]]] = []  # not drivable for an ambulance

    def node(self, n):
        t = n.tags
        if t.get("place") in PLACES and t.get("name"):
            self.places.append((t["place"], t["name"], n.location.lat, n.location.lon))
        k = self._poi(t)
        if k:
            self.pois.append((k, t.get("name", ""), n.location.lat, n.location.lon))

    @staticmethod
    def _poi(t):
        a = t.get("amenity")
        return {"hospital": "hospital", "fire_station": "fire", "police": "police", "clinic": "clinic",
                "fuel": "fuel"}.get(a)

    def way(self, w):
        t = w.tags
        k = self._poi(t)
        if k:
            try:
                la = [n.lat for n in w.nodes]; lo = [n.lon for n in w.nodes]
                self.pois.append((k, t.get("name", ""), sum(la) / len(la), sum(lo) / len(lo)))
            except osmium.InvalidLocationError:
                pass
        hw = t.get("highway")
        if not hw:
            return
        base = hw[:-5] if hw.endswith("_link") else hw
        private = t.get("access") in ("private", "no")
        if base == "track" or (private and base in ("service", "unclassified", "residential")):
            try:
                self.tracks.append((t.get("name", ""), [(n.lat, n.lon) for n in w.nodes]))
            except osmium.InvalidLocationError:
                pass
            return
        if base not in ROAD_SPEEDS_KMH or private:
            return
        speed = ROAD_SPEEDS_KMH[base] / 3.6
        oneway = base == "motorway" or t.get("junction") == "roundabout" or t.get("oneway") in ("yes", "1", "true")
        rev = t.get("oneway") == "-1"
        ref = (t.get("ref") or "").replace(" ", "").replace("-", "")
        nodes = list(w.nodes)
        for a, b in zip(nodes, nodes[1:]):
            try:
                la1, lo1, la2, lo2 = a.lat, a.lon, b.lat, b.lon
            except osmium.InvalidLocationError:
                continue
            self.coords[a.ref] = (la1, lo1); self.coords[b.ref] = (la2, lo2)
            g = ""
            for gid, gh in GHATS.items():
                if gh["ref"] in ref.split(";") and in_box((la1 + la2) / 2, (lo1 + lo2) / 2, gh["box"]):
                    g = gid
            ts = haversine_m(la1, lo1, la2, lo2) / speed
            if rev:
                self.edges.append((b.ref, a.ref, ts, g))
            else:
                self.edges.append((a.ref, b.ref, ts, g))
                if not oneway:
                    self.edges.append((b.ref, a.ref, ts, g))


def build_graph(h: Handler):
    ids = np.fromiter(h.coords.keys(), dtype=np.int64)
    idx = {n: i for i, n in enumerate(ids)}
    e = pd.DataFrame(h.edges, columns=["u", "v", "t", "ghat"])
    e["u"] = e.u.map(idx); e["v"] = e.v.map(idx)
    e = e[e.u != e.v].sort_values("t").drop_duplicates(["u", "v"])
    n = len(ids)
    adj = sp.csr_matrix((e.t.values, (e.u.values, e.v.values)), shape=(n, n))
    _, lab = csg.connected_components(adj, directed=True, connection="strong")
    keep = lab == np.bincount(lab).argmax()
    new = -np.ones(n, dtype=np.int64); new[keep] = np.arange(keep.sum())
    e = e[keep[e.u.values] & keep[e.v.values]].copy()
    e["u"] = new[e.u.values]; e["v"] = new[e.v.values]
    lat = np.array([h.coords[i][0] for i in ids[keep]]); lon = np.array([h.coords[i][1] for i in ids[keep]])
    nodes = pd.DataFrame({"osm_id": ids[keep], "lat": lat, "lon": lon})
    return nodes, e.reset_index(drop=True)


def kd(lat, lon):
    return cKDTree(np.c_[lat, np.asarray(lon) * np.cos(np.radians(13))])


def snap(tree, lat, lon):
    return tree.query(np.c_[lat, np.asarray(lon) * np.cos(np.radians(13))])[1]


# ----------------------------------------------------------------------------- scenarios
SCENARIOS = {
    # key: (label, monsoon, closed ghats, probability weight in the plan)
    "dry": ("Dry season (Oct-May)", False, (), 1 - P_MONSOON),
    "monsoon": ("Monsoon, ghats open", True, (), P_MONSOON * (1 - P_SHIRADI_SHUT_IN_MONSOON)),
    "shiradi": ("Monsoon, landslide shuts Shiradi", True, ("shiradi",), P_MONSOON * P_SHIRADI_SHUT_IN_MONSOON),
    "both": ("Monsoon, Shiradi and Charmadi both shut", True, ("shiradi", "charmadi"), 0.0),  # stress test only
}


def landslide_spots(nodes, edges):
    """One modelled slip per ghat: the middle of the ghat section (≈, real slips move year to year)."""
    spots = {}
    for gid in GHATS:
        g = edges[edges.ghat == gid]
        u = np.unique(np.r_[g.u.values, g.v.values])
        la, lo = nodes.lat.values[u], nodes.lon.values[u]
        c = np.argsort(lo)[len(lo) // 2]  # halfway along the ghat (both run roughly west-east)
        spots[gid] = (float(la[c]), float(lo[c]))
    return spots


def scenario_weights(nodes, edges, key, spots):
    """Ambulance seconds per edge under a scenario; closed edges are dropped (weight 0 = no edge in csgraph)."""
    _, monsoon, closed, _ = SCENARIOS[key]
    t = edges.t.values * RURAL_FLAT_FACTOR / AMBULANCE_SPEED_MULTIPLIER
    if monsoon:
        t = t * np.where(edges.ghat.values != "", MONSOON_GHAT_SLOWDOWN, MONSOON_GENERAL_SLOWDOWN)
    keep = np.ones(len(edges), bool)
    for gid in closed:
        la, lo = spots[gid]
        mla = (nodes.lat.values[edges.u.values] + nodes.lat.values[edges.v.values]) / 2
        mlo = (nodes.lon.values[edges.u.values] + nodes.lon.values[edges.v.values]) / 2
        d = np.hypot(mla - la, (mlo - lo) * np.cos(np.radians(13))) * 111_000
        keep &= ~((edges.ghat.values == gid) & (d < LANDSLIDE_RADIUS_M))
    n = len(nodes)
    return sp.csr_matrix((t[keep], (edges.u.values[keep], edges.v.values[keep])), shape=(n, n))


# ----------------------------------------------------------------------------- robust MEXCLP
def solve_robust_mexclp(cover, lam, weights, p_units, q=0.3, cap=2, levels=2):
    """Maximise sum_s w_s sum_i lam_si * sum_k (1-q) q^(k-1) z_ski   (MEXCLP, Daskin 1983, one per scenario)

    cover[s]: bool (cells x sites), lam[s]: calls per cell, weights[s]: scenario weight.
    Standby counts x_j are shared by all scenarios, so the plan must hold up in each of them.
    z_ski <= 1;  sum_k z_ski <= sum_j cover[s][i,j] x_j;  sum_j x_j = p_units;  x_j in {0..cap}.
    """
    S, (I, J) = len(cover), cover[0].shape
    nz = S * levels * I
    nv = J + nz
    c = np.zeros(nv)
    for s in range(S):
        for k in range(levels):
            off = J + (s * levels + k) * I
            c[off:off + I] = -weights[s] * lam[s] * (1 - q) * q ** k
    rows = []
    for s in range(S):
        A = sp.lil_matrix((I, nv))
        A[:, :J] = -cover[s].astype(float)
        for k in range(levels):
            off = J + (s * levels + k) * I
            A[np.arange(I), off + np.arange(I)] = 1.0
        rows.append(A.tocsr())
    A = sp.vstack(rows + [sp.csr_matrix((np.r_[np.ones(J), np.zeros(nz)])[None, :])])
    lb = np.r_[np.full(S * I, -np.inf), p_units]
    ub = np.r_[np.zeros(S * I), p_units]
    integ = np.r_[np.ones(J), np.zeros(nz)]
    res = milp(c, constraints=LinearConstraint(A, lb, ub), integrality=integ,
               bounds=Bounds(np.zeros(nv), np.r_[np.full(J, cap), np.ones(nz)]), options={"time_limit": 60})
    assert res.success, res.message
    return np.round(res.x[:J]).astype(int)


def self_check():
    # Two cells, two sites. Scenario A: site 0 covers cell 0, site 1 covers cell 1. Scenario B: site 0 cut off.
    cover = [np.array([[1, 0], [0, 1]], bool), np.array([[0, 0], [0, 1]], bool)]
    lam = [np.array([1.0, 1.0]), np.array([1.0, 1.0])]
    assert list(solve_robust_mexclp(cover, lam, [1, 0], 1, cap=1)) in ([1, 0], [0, 1])
    assert list(solve_robust_mexclp(cover, lam, [0.5, 0.5], 1, cap=1)) == [0, 1]  # robust picks the safe site
    print("rural self-check ok")


# ----------------------------------------------------------------------------- routing helpers
def route(G, src, dst, nodes):
    d, pred = csg.dijkstra(G, indices=src, return_predecessors=True)
    if not np.isfinite(d[dst]):
        return None
    path = [dst]
    while path[-1] != src:
        path.append(pred[path[-1]])
    path = path[::-1]
    return {"minutes": float(d[dst] / 60), "path": path}


def leg(r, nodes, no_signal_edge, kind, label):
    if r is None:
        return {"kind": kind, "label": label, "blocked": True}
    p = r["path"]
    la, lo = nodes.lat.values[p], nodes.lon.values[p]
    km = float(sum(haversine_m(la[i], lo[i], la[i + 1], lo[i + 1]) for i in range(len(p) - 1)) / 1000)
    coords = [[round(float(x), 5), round(float(y), 5)] for x, y in zip(lo, la)]
    # no-signal stretches as separate polylines
    bad, runs, cur = [no_signal_edge.get((p[i], p[i + 1]), False) for i in range(len(p) - 1)], [], []
    for i, b in enumerate(bad):
        if b:
            cur = cur or [coords[i]]
            cur.append(coords[i + 1])
        elif cur:
            runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    dead_km = sum(LineString([(a[1], a[0]) for a in r_]).length * 111 for r_ in runs if len(r_) > 1)
    return {"kind": kind, "label": label, "minutes": round(r["minutes"], 1), "km": round(km, 1),
            "coords": coords[:: max(1, len(coords) // 600)] + [coords[-1]],
            "no_signal": [r_[:: max(1, len(r_) // 150)] + [r_[-1]] for r_ in runs], "no_signal_km": round(dead_km, 1)}


# ----------------------------------------------------------------------------- main build
def main():
    t0 = time.time()
    if not SKP_PBF.exists():
        subprocess.run(["osmium", "extract", "-b", ",".join(map(str, BBOX)), "-o", str(SKP_PBF),
                        str(RAW_DIR / "southern-zone.osm.pbf"), "--overwrite"], check=True)
    h = Handler()
    h.apply_file(str(SKP_PBF), locations=True)
    nodes, edges = build_graph(h)
    print(f"[rural] graph {len(nodes):,} nodes, {len(edges):,} edges ({time.time() - t0:.0f}s)")
    for gid in GHATS:
        g = edges[edges.ghat == gid]
        km = sum(haversine_m(*nodes.iloc[u][["lat", "lon"]], *nodes.iloc[v][["lat", "lon"]]) for u, v in zip(g.u, g.v)) / 2000
        print(f"[rural] {gid}: {km:.1f} km of ghat road tagged")
        assert km > 10, f"{gid} ghat not found; check GHATS box/ref"
    tree = kd(nodes.lat.values, nodes.lon.values)

    # ---- taluk boundary and service area (taluk + Shiradi corridor down to Gundya)
    adm_pbf = RAW_DIR / "skp_admin.osm.pbf"
    subprocess.run(["osmium", "tags-filter", str(SKP_PBF), "r/boundary=administrative", "-o", str(adm_pbf),
                    "--overwrite"], check=True)
    adm = subprocess.run(["osmium", "export", str(adm_pbf), "-f", "geojsonseq", "--geometry-types=polygon"],
                         capture_output=True, check=True).stdout.decode().split("\x1e")
    taluk = next(shape(d["geometry"]) for d in map(json.loads, filter(str.strip, adm))
                 if d["properties"].get("name") == TALUK and d["properties"].get("admin_level") == "6")
    sh = edges[edges.ghat == "shiradi"]
    corridor = unary_union([LineString([(nodes.lon[u], nodes.lat[u]), (nodes.lon[v], nodes.lat[v])])
                            for u, v in zip(sh.u, sh.v)]).buffer(CORRIDOR_BUFFER_DEG)
    area = unary_union([taluk, corridor]).simplify(0.001)

    # ---- cells, population, synthetic demand from Tumakuru
    cells = sorted(h3.geo_to_cells(area.__geo_interface__, H3_RES))
    cl = np.array([h3.cell_to_latlng(c) for c in cells])
    pop = sample_population(cells, H3_RES, RAW_DIR / "ind_ppp_2020_1km_Aggregated.tif")
    tmk_calls = pd.read_parquet(PROCESSED_DIR / "calls_tmk.parquet")
    tmk_pop = pd.read_parquet(PROCESSED_DIR / "cells_tmk.parquet")["pop"].sum()
    per_person_day = len(tmk_calls) / 365 / tmk_pop
    mix = tmk_calls.call_type.value_counts(normalize=True)
    hourly = tmk_calls.hour.value_counts(normalize=True).sort_index().values
    # trauma follows major roads (same rule as demand.py: 0.5 population + 0.5 major-road length)
    road_tree = kd(nodes.lat.values[edges.u.values], nodes.lon.values[edges.u.values])
    ghat_or_nh = (edges.ghat.values != "")
    near = road_tree.query_ball_point(np.c_[cl[:, 0], cl[:, 1] * np.cos(np.radians(13))], r=0.005)
    major = np.array([ghat_or_nh[n].sum() if n else 0 for n in near], float)
    daily = per_person_day * pop.sum()
    w_med = pop / pop.sum()
    w_tr = 0.5 * w_med + 0.5 * (major / major.sum() if major.sum() else w_med)
    lam_day = daily * ((1 - mix["trauma"]) * w_med + mix["trauma"] * w_tr)  # calls/day per cell
    rng = np.random.default_rng(7)
    rows = []
    for d in pd.date_range("2025-11-01", periods=365):
        f = 1.12 if d.month in (6, 7, 8, 9) else 1.0  # same monsoon trauma bump as demand.py (≈)
        n = rng.poisson(lam_day * f)
        for i in np.nonzero(n)[0]:
            for _ in range(n[i]):
                hr = rng.choice(24, p=hourly)
                rows.append((d + pd.Timedelta(hours=int(hr), minutes=int(rng.integers(60))), cells[i],
                             rng.choice(mix.index, p=mix.values)))
    calls = pd.DataFrame(rows, columns=["timestamp", "h3_index", "call_type"])
    calls.to_parquet(OUT / "calls_skp.parquet")
    print(f"[rural] {len(cells)} cells, population {pop.sum():,.0f}, synthetic calls {len(calls):,}/yr "
          f"({daily:.1f}/day at Tumakuru's rate)")

    # ---- standby sites inside the service area (one per cell, hospital > fire > police > clinic > fuel)
    pl = pd.DataFrame([(("town" if k in ("city", "town") else "village"), n, a, b) for k, n, a, b in h.places
                       if k != "hamlet"], columns=["type", "name", "lat", "lon"])
    pois = pd.concat([pd.DataFrame(h.pois, columns=["type", "name", "lat", "lon"]), pl], ignore_index=True)
    pois = pois[[area.contains(Point(lo, la)) for la, lo in zip(pois.lat, pois.lon)]].copy()
    pois["cell"] = [h3.latlng_to_cell(a, b, H3_RES) for a, b in zip(pois.lat, pois.lon)]
    pois["rank"] = pois.type.map(SITE_TYPES)
    sites = pois.sort_values(["rank"]).drop_duplicates("cell").reset_index(drop=True)
    sites["node"] = snap(tree, sites.lat, sites.lon)
    cell_nodes = snap(tree, cl[:, 0], cl[:, 1])
    print(f"[rural] {len(sites)} candidate standby sites")

    # ---- dead zones (modelled): ghat edges, or edge midpoint > NO_SIGNAL_KM from any village/town
    places = pd.DataFrame(h.places, columns=["kind", "name", "lat", "lon"])
    ptree = kd(places.lat.values, places.lon.values)
    mla = (nodes.lat.values[edges.u] + nodes.lat.values[edges.v]) / 2
    mlo = (nodes.lon.values[edges.u] + nodes.lon.values[edges.v]) / 2
    far = ptree.query(np.c_[mla, mlo * np.cos(np.radians(13))])[0] * 111 > NO_SIGNAL_KM
    dead = far | (edges.ghat.values != "")
    no_signal_edge = {(int(u), int(v)): True for u, v in zip(edges.u.values[dead], edges.v.values[dead])}

    spots = landslide_spots(nodes, edges)
    G = {k: scenario_weights(nodes, edges, k, spots) for k in SCENARIOS}

    # ---- travel site -> cell per scenario, and the robust plan
    reach = {}
    for k in SCENARIOS:
        D = csg.dijkstra(G[k], indices=sites.node.values, limit=3 * 3600)
        reach[k] = (D[:, cell_nodes].T / 60)  # cells x sites, minutes
    # Rural reality: the 10-min target is out of reach for most of the taluk with 3 units, so the plan also
    # rewards a 20-min second ring (half weight) to keep it from abandoning far villages.
    def cover_of(k, mins):
        return reach[k] <= mins - PRE_TRAVEL_MIN
    keys = ["dry", "monsoon", "shiradi"]
    lam_s = {k: lam_day * (1.12 if SCENARIOS[k][1] else 1.0) for k in SCENARIOS}

    def plan(weights):
        cov = [cover_of(k, 20) for k in keys]
        return solve_robust_mexclp(cov, [lam_s[k] for k in keys], weights, FLEET_TALUK)

    x_dry = plan([1, 0, 0])
    x_rob = plan([SCENARIOS[k][3] for k in keys])

    def kpis(x):
        out = {}
        for k in SCENARIOS:
            j = np.repeat(np.arange(len(x)), x)
            best = reach[k][:, j].min(axis=1) + PRE_TRAVEL_MIN
            w = lam_s[k] / lam_s[k].sum()
            out[k] = {"within_10": round(float(w[best <= 10].sum() * 100), 1),
                      "within_20": round(float(w[best <= 20].sum() * 100), 1),
                      "within_30": round(float(w[best <= 30].sum() * 100), 1),
                      "median": round(float(np.median(np.repeat(best, np.maximum(1, (w * 1000).astype(int))))), 1)}
        return out, best
    k_dry, _ = kpis(x_dry)
    k_rob, _ = kpis(x_rob)
    # villages cut off: response time from the taluk's own units rises by > 15 min when a ghat shuts
    j_rob = np.repeat(np.arange(len(x_rob)), x_rob)
    base_t = reach["monsoon"][:, j_rob].min(axis=1)
    cutoff = {k: [cells[i] for i in np.nonzero(reach[k][:, j_rob].min(axis=1) - base_t > 15)[0]]
              for k in ("shiradi", "both")}
    print("[rural] cells cut off:", {k: len(v) for k, v in cutoff.items()})
    print("[rural] dry-season plan:", k_dry)
    print("[rural] monsoon-proof plan:", k_rob)

    # ---- hospitals
    hp = pd.DataFrame(h.pois, columns=["type", "name", "lat", "lon"])
    hp = hp[hp.type == "hospital"]
    hosp = []
    for hid, match, label, tier, caps in HOSPITALS:
        m = hp[hp.name.str.contains(match, case=False, regex=False)]
        if hid == "hassan_dh":
            m = m[(m.lon > 76.05) & (m.lat > 12.98)]
        if m.empty:
            print(f"[rural] WARNING hospital not in OSM: {match}"); continue
        r = m.iloc[0]
        hosp.append({"id": hid, "name": label, "osm_name": r["name"], "lat": round(r.lat, 5), "lon": round(r.lon, 5),
                     "tier": tier, "caps": caps, "node": int(snap(tree, [r.lat], [r.lon])[0])})
    H = {x["id"]: x for x in hosp}

    def pt(lat, lon):
        return int(snap(tree, [lat], [lon])[0])

    units = [{"id": f"SKP-{i + 1}", "lat": float(sites.lat[j]), "lon": float(sites.lon[j]),
              "site": sites.name[j] or sites.type[j], "node": int(sites.node[j]), "als": i == 0}
             for i, j in enumerate(np.repeat(np.arange(len(x_rob)), x_rob))]
    nb_node = pt(NEIGHBOUR_UNIT[1], NEIGHBOUR_UNIT[2])
    units.append({"id": "KDB-1", "lat": NEIGHBOUR_UNIT[1], "lon": NEIGHBOUR_UNIT[2], "site": NEIGHBOUR_UNIT[0],
                  "node": nb_node, "als": False, "neighbour": True})

    def nearest_unit(G_, dst, pool):
        best = None
        for u in pool:
            r = route(G_, u["node"], dst, nodes)
            if r and (best is None or r["minutes"] < best[1]["minutes"]):
                best = (u, r)
        return best

    # ---- demo cases, each under every scenario
    # Case 1: crash on the lower Shiradi ghat, below the modelled slip
    sl = spots["shiradi"]
    gh = edges[edges.ghat == "shiradi"]
    gu = np.unique(gh.u.values)
    below = gu[(nodes.lon.values[gu] < sl[1] - 0.01) & (nodes.lon.values[gu] > sl[1] - 0.03)]  # just downhill of the slip
    crash = int(below[np.argsort(nodes.lon.values[below])[len(below) // 2]])
    # Case 2: chest pain in a western village (Hanbal side)
    chest = pt(13.0058, 75.6971)
    # Case 3: road crash near Alur, nearest ICU refuses
    alur_crash = pt(12.98, 75.95)
    # Case 4: patient up an estate track: pick the longest track within the taluk
    tr = [(n, c) for n, c in h.tracks if len(c) > 5 and taluk.contains(Point(c[-1][1], c[-1][0]))]
    tr.sort(key=lambda t: -LineString([(b, a) for a, b in t[1]]).length)
    est_name, est_coords = tr[0]
    est_pt = est_coords[-1]
    est_meet = pt(*est_pt)

    cases = {"crash_ghat": {}, "chest_pain": {}, "icu_refusal": {}, "estate": {}}
    for k in SCENARIOS:
        Gk = G[k]
        # 1. crash on the ghat
        own = [u for u in units if not u.get("neighbour")]
        u_own, r_own = nearest_unit(Gk, crash, own) or (own[0], None)
        u_any, r_any = nearest_unit(Gk, crash, units)
        to_h = min(((x, route(Gk, crash, x["node"], nodes)) for x in hosp if x["tier"] == "hub" or x["id"] in
                    ("subrahmanya", "crawford")), key=lambda t: t[1]["minutes"] if t[1] else 1e9)
        legs = []
        if u_any["id"] != u_own["id"]:
            legs.append(leg(r_own, nodes, no_signal_edge, "rejected", f"{u_own['id']} from {u_own['site']} (taluk unit)"))
        legs.append(leg(r_any, nodes, no_signal_edge, "unit", f"{u_any['id']} from {u_any['site']}"))
        legs.append(leg(to_h[1], nodes, no_signal_edge, "hospital", f"To {to_h[0]['name']} (stabilise)"))
        cases["crash_ghat"][k] = {
            "legs": legs, "unit": u_any["id"], "hospital": to_h[0]["id"],
            "own_unit_min": round(r_own["minutes"], 1) if r_own else None,
            "chosen_unit_min": round(r_any["minutes"], 1),
            "cross_border": bool(u_any.get("neighbour")),
        }
        # 2. chest pain: thrombolysis at the taluk spoke, then hub; vs straight to hub
        u2, r2 = nearest_unit(Gk, chest, units[:-1])
        spoke = route(Gk, chest, H["crawford"]["node"], nodes)
        hub_direct = route(Gk, chest, H["hassan_dh"]["node"], nodes)
        spoke_hub = route(Gk, H["crawford"]["node"], H["hassan_dh"]["node"], nodes)
        to_mlr = route(Gk, H["crawford"]["node"], H["wenlock"]["node"], nodes)
        first_contact = r2["minutes"] + PRE_TRAVEL_MIN
        drug_at_spoke = first_contact + spoke["minutes"] + 30   # ≈ door-to-needle 30 min (guideline target)
        straight = first_contact + hub_direct["minutes"] + 30
        cases["chest_pain"][k] = {
            "legs": [leg(r2, nodes, no_signal_edge, "unit", f"{u2['id']} from {u2['site']}"),
                     leg(spoke, nodes, no_signal_edge, "hospital", "To Crawford Govt Hospital (clot-busting drug)"),
                     leg(spoke_hub, nodes, no_signal_edge, "transfer", "Transfer to District Hospital, Hassan"),
                     leg(to_mlr, nodes, no_signal_edge, "backup", "Mangaluru (Wenlock) by road, if Hassan is full")],
            "drug_at_spoke_min": round(drug_at_spoke), "drug_at_hub_min": round(straight),
            "mangaluru_min": round(to_mlr["minutes"]) if to_mlr else None,
        }
        # 3. ICU refusal: pre-alert to Hassan DH comes back "no ICU bed" before the ambulance leaves the scene
        u3, r3 = nearest_unit(Gk, alur_crash, units[:-1])
        first = route(Gk, alur_crash, H["hassan_dh"]["node"], nodes)
        second = route(Gk, alur_crash, H["hassan_mani"]["node"], nodes)
        bounce = route(Gk, H["hassan_dh"]["node"], H["hassan_mani"]["node"], nodes)
        cases["icu_refusal"][k] = {
            "legs": [leg(r3, nodes, no_signal_edge, "unit", f"{u3['id']} from {u3['site']}"),
                     leg(first, nodes, no_signal_edge, "rejected", "District Hospital, Hassan: no ICU bed"),
                     leg(second, nodes, no_signal_edge, "hospital", "Mani Super Speciality, Hassan (accepted)")],
            "with_prealert_min": round(second["minutes"], 1),
            "without_prealert_min": round(first["minutes"] + 10 + bounce["minutes"], 1),  # ≈ 10 min at the door
        }
        # 4. estate: ambulance drives to the meeting point, last stretch by estate jeep or stretcher
        u4, r4 = nearest_unit(Gk, est_meet, units[:-1])
        to_c = route(Gk, est_meet, H["crawford"]["node"], nodes)
        mlat, mlon = nodes.lat[est_meet], nodes.lon[est_meet]
        cases["estate"][k] = {
            "legs": [leg(r4, nodes, no_signal_edge, "unit", f"{u4['id']} from {u4['site']}"),
                     leg(to_c, nodes, no_signal_edge, "hospital", "To Crawford Govt Hospital")],
            "patient": [round(est_pt[1], 5), round(est_pt[0], 5)],
            "meet": [round(float(mlon), 5), round(float(mlat), 5)],
            "track": [[round(b, 5), round(a, 5)] for a, b in est_coords],
            "last_km": round(haversine_m(est_pt[0], est_pt[1], mlat, mlon) / 1000, 1),
        }

    ghat_lines = {gid: [[[round(nodes.lon[u], 5), round(nodes.lat[u], 5)], [round(nodes.lon[v], 5), round(nodes.lat[v], 5)]]
                        for u, v in zip(*edges[edges.ghat == gid][["u", "v"]].values.T) if u < v]
                  for gid in GHATS}
    # hairpin warning spots: sharp turns on the ghat (≈ heading change > 100 degrees within ~150 m)
    hairpins = []
    for gid in GHATS:
        g = edges[edges.ghat == gid]
        nxt = dict(zip(g.u, g.v))
        for u, v in nxt.items():
            w = nxt.get(v)
            if w is None or w == u:
                continue
            a = np.degrees(np.arctan2(nodes.lat[v] - nodes.lat[u], nodes.lon[v] - nodes.lon[u]))
            b = np.degrees(np.arctan2(nodes.lat[w] - nodes.lat[v], nodes.lon[w] - nodes.lon[v]))
            if abs((b - a + 180) % 360 - 180) > 100:
                hairpins.append([round(nodes.lon[v], 5), round(nodes.lat[v], 5)])
    keep_hp = []
    for p_ in hairpins:  # thin to one sign per ~1.5 km
        if all(haversine_m(p_[1], p_[0], q[1], q[0]) > 1500 for q in keep_hp):
            keep_hp.append(p_)

    out = {
        "region": "Sakleshpura taluk and the Shiradi Ghat corridor",
        "target_min": TARGET_MIN,
        "population": round(float(pop.sum())),
        "calls_per_day": round(float(daily), 1),
        "fleet": FLEET_TALUK,
        "scenarios": {k: {"label": v[0], "weight": round(v[3], 3)} for k, v in SCENARIOS.items()},
        "area": json.loads(json.dumps(area.__geo_interface__)),
        "cells": [{"h": c, "pop": round(float(p)), "lam": round(float(l), 4)} for c, p, l in zip(cells, pop, lam_day)],
        "ghats": {gid: {"name": GHATS[gid]["name"], "lines": ghat_lines[gid],
                        "landslide": [round(spots[gid][1], 5), round(spots[gid][0], 5)]} for gid in GHATS},
        "hairpins": keep_hp,
        "hospitals": [{k: v for k, v in x.items() if k != "node"} for x in hosp],
        "units": [{k: v for k, v in u.items() if k != "node"} for u in units],
        "plans": {
            "dry": {"sites": [{"lat": float(sites.lat[j]), "lon": float(sites.lon[j]), "name": sites.name[j] or sites.type[j],
                               "n": int(x_dry[j])} for j in np.nonzero(x_dry)[0]], "kpis": k_dry},
            "robust": {"sites": [{"lat": float(sites.lat[j]), "lon": float(sites.lon[j]), "name": sites.name[j] or sites.type[j],
                                  "n": int(x_rob[j])} for j in np.nonzero(x_rob)[0]], "kpis": k_rob},
        },
        "cases": cases,
        "cutoff": cutoff,
        "estate_name": est_name,
        "assumptions": [
            ["Roads, hospitals, villages, taluk boundary", "OpenStreetMap (real)"],
            ["Population", "WorldPop 2020, 1 km (real)"],
            ["Calls per day", f"≈ Tumakuru's synthetic rate ({per_person_day * 1e5:.1f} per lakh people per day) x Sakleshpura population"],
            ["Fleet", f"≈ {FLEET_TALUK} ambulances (Tumakuru's ~1 per 350 km²) plus one unit in the next taluk"],
            ["Monsoon slow-down", f"≈ ghat roads x{MONSOON_GHAT_SLOWDOWN}, other roads x{MONSOON_GENERAL_SLOWDOWN}"],
            ["Landslide spot", "≈ middle of each ghat section; ◐ Shiradi slips reported 2024 and 2025"],
            ["Chance Shiradi is shut", f"≈ {int(P_SHIRADI_SHUT_IN_MONSOON * 100)}% of the monsoon"],
            ["No mobile signal", f"≈ ghat sections and roads more than {NO_SIGNAL_KM:.0f} km from any village"],
            ["Hospital capabilities", "≈ assumed by hospital type; to be checked with each hospital"],
            ["Door-to-needle time", "≈ 30 min (guideline target)"],
        ],
    }
    WEB_JSON.write_text(json.dumps(out, separators=(",", ":")))
    nodes.to_parquet(OUT / "graph_nodes_skp.parquet"); edges.to_parquet(OUT / "graph_edges_skp.parquet")
    print(f"[rural] wrote {WEB_JSON} ({WEB_JSON.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    self_check() if "--check" in sys.argv else main()
