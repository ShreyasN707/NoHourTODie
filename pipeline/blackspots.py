"""GoldenHour: black spots, where the ambulance could not reach in time, why, and where to place units instead.

1. Find. Each dashboard plan is replayed call by call (pipeline/simulate.py) over BLACKSPOT_WINDOW_DAYS. A hexagon
   is a black spot when it has at least BLACKSPOT_MIN_LATE late calls and, with ~95% confidence (Wilson lower
   bound), at least BLACKSPOT_MIN_LATE_SHARE of its calls were late. It is persistent when it was also a black
   spot in the window before (black spots on short windows are noisy: few calls per hexagon).
2. Explain. Every late call gets one cause, from two counterfactual drive times the replay records:
     unreachable: no candidate standby site, staffed or not, reaches the hexagon within the target
     uncovered:   a candidate site could, but the plan staffs none within reach in that block
     busy:        a planned unit is within reach, but it was out on another job (or queued)
3. Fix, in three cumulative steps, each re-planned with the same MEXCLP MILP and re-simulated:
     targeted: same fleet, same sites. Black-spot hexagons get more weight in the objective, re-solved for a
               few feedback rounds (multiplicative weights from each round's replay)
     posts:    plus a handful of new standby posts at unreachable black spots, chosen by greedy maximum
               coverage of their late calls (drive times by Dijkstra on the OSM road graph)
     fleet:    plus extra ambulances
4. Hold-out. Weights and posts are chosen on the window *before* the evaluation window. Every number shown
   is from the last window, which the fixes never saw.

All calls are synthetic (the GoldenHour data package), so every result is a simulation result.
Writes web/public/data/blackspots_{region}.json. Run: make blackspots (or --quick for short solver limits).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import h3
import numpy as np
import pandas as pd
import scipy.sparse as sp
import scipy.sparse.csgraph as csg

from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    BENGALURU_HOURLY_CONGESTION,
    BLACKSPOT_ALPHA,
    BLACKSPOT_FLEET_STEPS,
    BLACKSPOT_MAX_CANDIDATE_POSTS,
    BLACKSPOT_MAX_NEW_POSTS,
    BLACKSPOT_MAX_WEIGHT,
    BLACKSPOT_MIN_LATE,
    BLACKSPOT_MIN_LATE_SHARE,
    BLACKSPOT_MIN_POST_GAIN,
    BLACKSPOT_POST_SPACING_RINGS,
    BLACKSPOT_ROUNDS,
    BLACKSPOT_WINDOW_DAYS,
    BLACKSPOT_Z,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    REGIONS,
    WEB_DATA_DIR,
)
from pipeline.optimize import BlockPlan, solve_goldenhour_rolling
from pipeline.simulate import replay_calls, summarize
from pipeline.travel import LIMIT_SECONDS

CAUSES = ("unreachable", "uncovered", "busy")
CAUSE_INFO = {
    "unreachable": {
        "label": "Out of reach",
        "detail": "No candidate standby site of any kind reaches it within the target, even with a unit waiting.",
        "fix": "Open a standby post nearby",
    },
    "uncovered": {
        "label": "No unit planned nearby",
        "detail": "A candidate site could reach it in time, but the plan stations no unit within reach.",
        "fix": "Re-place a unit",
    },
    "busy": {
        "label": "Nearby unit busy",
        "detail": "A unit is planned within reach, but it was out on another job when the call came.",
        "fix": "Add a unit, or a second one nearby",
    },
}
SEVERITIES = ("moderate", "high", "severe")


# ---------------------------------------------------------------------------------------------- detection


def wilson_lower(k: np.ndarray, n: np.ndarray, z: float = BLACKSPOT_Z) -> np.ndarray:
    """Lower bound of the Wilson score interval for a share of k out of n (0 where n == 0)."""
    k = np.asarray(k, dtype=float)
    n = np.asarray(n, dtype=float)
    out = np.zeros_like(n)
    m = n > 0
    p = k[m] / n[m]
    denom = 1.0 + z * z / n[m]
    centre = p + z * z / (2 * n[m])
    spread = z * np.sqrt(p * (1 - p) / n[m] + z * z / (4 * n[m] ** 2))
    out[m] = np.clip((centre - spread) / denom, 0.0, 1.0)
    return out


def classify_calls(log: pd.DataFrame, target_min: float) -> pd.DataFrame:
    """Add late, excess_min (minutes beyond target) and cause (for late calls) to a replay log."""
    log = log.copy()
    log["late"] = log["response_min"] > target_min
    log["excess_min"] = np.maximum(0.0, log["response_min"] - target_min)
    cause = np.where(log["best_min"] > target_min, "unreachable",
                     np.where(log["planned_min"] > target_min, "uncovered", "busy"))
    log["cause"] = np.where(log["late"], cause, "")
    return log


def cell_stats(log: pd.DataFrame, n_cells: int, target_min: float) -> pd.DataFrame:
    """Per-hexagon replay statistics and black-spot flags, indexed 0..n_cells-1 (cells without calls kept)."""
    g = log.groupby("cell_idx")
    st = pd.DataFrame(index=pd.RangeIndex(n_cells, name="cell_idx"))
    st["calls"] = g.size()
    st["late"] = g["late"].sum()
    st["excess_min"] = g["excess_min"].sum()
    st["p90_min"] = g["response_min"].quantile(0.9)
    for c in CAUSES:
        st[f"n_{c}"] = log[log["cause"] == c].groupby("cell_idx").size()
    st = st.fillna(0.0)
    st["calls"] = st["calls"].astype(int)
    st["late"] = st["late"].astype(int)
    for c in CAUSES:
        st[f"n_{c}"] = st[f"n_{c}"].astype(int)
    st["late_share"] = np.where(st["calls"] > 0, st["late"] / st["calls"].clip(lower=1), 0.0)
    st["late_lo"] = wilson_lower(st["late"].to_numpy(), st["calls"].to_numpy())
    st["black"] = (st["late"] >= BLACKSPOT_MIN_LATE) & (st["late_lo"] >= BLACKSPOT_MIN_LATE_SHARE)
    counts = st[[f"n_{c}" for c in CAUSES]].to_numpy()
    st["cause"] = np.where(st["late"] > 0, np.array(CAUSES)[counts.argmax(axis=1)], "")
    # Severity: how late the slowest calls there are (90th percentile response vs the target)
    st["severity"] = np.select([st["p90_min"] > 2.0 * target_min, st["p90_min"] > 1.5 * target_min],
                               ["severe", "high"], "moderate")
    st.loc[~st["black"], "severity"] = ""
    return st


def cluster_black_spots(st: pd.DataFrame, df_cells: pd.DataFrame) -> List[Dict[str, Any]]:
    """Group touching black-spot hexagons (H3 neighbours) into clusters, worst (most minutes late) first."""
    black = st.index[st["black"]].to_numpy()
    h3s = df_cells["h3_index"].to_numpy()
    pos = {h3s[i]: i for i in black}
    parent = {i: i for i in black}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in black:
        for nb in h3.grid_disk(h3s[i], 1):
            j = pos.get(nb)
            if j is not None:
                parent[find(i)] = find(j)

    groups: Dict[int, List[int]] = {}
    for i in black:
        groups.setdefault(find(i), []).append(int(i))

    clusters = []
    for members in groups.values():
        sub = st.loc[members]
        cells = df_cells.iloc[members]
        w = sub["calls"].to_numpy(dtype=float)
        cause_tot = {c: int(sub[f"n_{c}"].sum()) for c in CAUSES}
        cause = max(cause_tot, key=cause_tot.get)
        zones = cells.assign(calls=w).groupby("zone")["calls"].sum().sort_values(ascending=False)
        late = int(sub["late"].sum())
        calls = int(sub["calls"].sum())
        clusters.append({
            "cells": sorted(members),
            "lat": round(float(np.average(cells["lat"], weights=w)), 5),
            "lon": round(float(np.average(cells["lon"], weights=w)), 5),
            "zones": [str(z) for z in zones.index[:3]],
            "calls": calls,
            "late": late,
            "late_pct": round(100.0 * late / max(calls, 1), 1),
            "excess_min": round(float(sub["excess_min"].sum()), 1),
            "mean_late_by_min": round(float(sub["excess_min"].sum()) / max(late, 1), 1),
            "worst_p90_min": round(float(sub["p90_min"].max()), 1),
            "causes": cause_tot,
            "cause": cause,
            "fix": CAUSE_INFO[cause]["fix"],
            "severity": max(sub["severity"], key=SEVERITIES.index),
        })
    clusters.sort(key=lambda c: -c["excess_min"])
    for k, c in enumerate(clusters, 1):
        c["id"] = k
    return clusters


# ------------------------------------------------------------------------------------------ plans and data


def plans_from_scenario(sc: Dict[str, Any], df_sites: pd.DataFrame) -> List[BlockPlan]:
    """Rebuild BlockPlans from a scenarios_{r}.json plan, so the black spots match the dashboard exactly."""
    idx = {s: i for i, s in enumerate(df_sites["site_id"])}
    plans = []
    for p in sc["plans"]:
        a = np.zeros(len(df_sites), np.int32)
        b = a.copy()
        y = a.copy()
        for d in p["deployments"]:
            i = idx[d["site_id"]]
            a[i], b[i], y[i] = d["a"], d["b"], d["y"]
        plans.append(BlockPlan(p["block_id"], a, b, y, p["q"], p["q"], None, None, None))
    return plans


def hour_minutes_per_second(reg: str) -> np.ndarray:
    """Ambulance minutes per free-flow second, by hour of day (same model as the simulation)."""
    cfg = REGIONS[reg]
    f = np.array(BENGALURU_HOURLY_CONGESTION if cfg.congestion_enabled else [1.10] * 24)
    return f / (AMBULANCE_SPEED_MULTIPLIER * 60.0)


class Region:
    def __init__(self, reg: str):
        self.reg = reg
        self.cfg = REGIONS[reg]
        self.cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg}.parquet")
        self.sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg}.parquet")
        self.calls = pd.read_parquet(PROCESSED_DIR / f"calls_{reg}.parquet")
        t = np.load(PROCESSED_DIR / f"travel_{reg}.npz")
        self.fwd, self.back, self.ss = t["fwd"], t["back"], t["ss"]
        path = WEB_DATA_DIR / f"scenarios_{reg}.json"
        if not path.exists():
            raise SystemExit(f"[blackspots] {path} missing: run `make package` (or `make export`) first.")
        self.scenarios = json.loads(path.read_text(encoding="utf-8"))

    def replay(self, plans, window: str, sites=None, fwd=None, back=None, ss=None) -> pd.DataFrame:
        log = replay_calls(
            self.reg, plans, self.calls, self.cells,
            self.sites if sites is None else sites,
            self.fwd if fwd is None else fwd,
            self.back if back is None else back,
            self.ss if ss is None else ss,
            sim_days=BLACKSPOT_WINDOW_DAYS,
            end_offset_days=BLACKSPOT_WINDOW_DAYS if window == "tune" else 0,
        )
        return classify_calls(log, self.cfg.target_min)


# -------------------------------------------------------------------------------------------- new posts


def load_graph() -> Tuple[sp.csr_matrix, sp.csr_matrix]:
    e = pd.read_parquet(PROCESSED_DIR / "graph_edges.parquet", columns=["u", "v", "time_s"])
    n = int(max(e["u"].max(), e["v"].max())) + 1
    adj = sp.csr_matrix((e["time_s"].to_numpy(np.float32), (e["u"].to_numpy(), e["v"].to_numpy())), shape=(n, n))
    return adj, adj.transpose().tocsr()


def post_travel(nodes: np.ndarray, targets: np.ndarray, adj, adj_t, chunk: int = 8) -> Tuple[np.ndarray, np.ndarray]:
    """Drive seconds new-node -> target-node and target-node -> new-node, (len(nodes), len(targets)) each."""
    out = np.empty((len(nodes), len(targets)), np.float32)
    inn = np.empty_like(out)
    for i in range(0, len(nodes), chunk):
        src = nodes[i:i + chunk]
        for g, dest in ((adj, out), (adj_t, inn)):
            d = csg.dijkstra(g, directed=True, indices=src, limit=LIMIT_SECONDS)[:, targets]
            dest[i:i + chunk] = np.where(np.isfinite(d) & (d < LIMIT_SECONDS), d, LIMIT_SECONDS)
    return out, inn


def propose_posts(R: Region, st_tune: pd.DataFrame, log_tune: pd.DataFrame, graph) -> Dict[str, Any]:
    """Pick new standby posts for unreachable black spots by greedy maximum coverage of their late calls.

    Candidates are the road nodes at the centres of black-spot hexagons whose late calls are mostly
    unreachable (thinned to BLACKSPOT_POST_SPACING_RINGS apart). A candidate covers a late call when a unit
    waiting there would arrive within the target at that call's hour. Posts are added while each one covers
    at least BLACKSPOT_MIN_POST_GAIN more late calls, up to BLACKSPOT_MAX_NEW_POSTS.
    """
    target = R.cfg.target_min
    seeds = st_tune[st_tune["black"] & (st_tune["cause"] == "unreachable")].sort_values("late", ascending=False)
    h3s = R.cells["h3_index"].to_numpy()
    picked: List[int] = []
    for i in seeds.index:
        if all(h3.grid_distance(h3s[i], h3s[j]) > BLACKSPOT_POST_SPACING_RINGS for j in picked):
            picked.append(int(i))
        if len(picked) >= BLACKSPOT_MAX_CANDIDATE_POSTS:
            break
    empty = {"sites": R.sites, "fwd": R.fwd, "back": R.back, "ss": R.ss, "posts": [], "candidates": len(picked)}
    if not picked:
        return empty

    adj, adj_t = graph
    cand_nodes = R.cells["node_idx"].to_numpy()[picked]
    cell_nodes = R.cells["node_idx"].to_numpy()
    site_nodes = R.sites["node_idx"].to_numpy()
    targets = np.concatenate([cell_nodes, site_nodes, cand_nodes])
    nc, ns = len(cell_nodes), len(site_nodes)
    t0 = time.time()
    out, inn = post_travel(cand_nodes, targets, adj, adj_t)
    print(f"[blackspots] Dijkstra for {len(picked)} candidate posts: {time.time() - t0:.1f}s")

    # Late calls that no existing site can reach in time; which candidates would reach them?
    late = log_tune[log_tune["cause"] == "unreachable"]
    mps = hour_minutes_per_second(R.reg)[late["hour"].to_numpy()]
    reach = PRE_TRAVEL_MIN + out[:, late["cell_idx"].to_numpy()] * mps <= target  # (cands, late calls)

    chosen, gains = [], []
    covered = np.zeros(reach.shape[1], bool)
    while len(chosen) < BLACKSPOT_MAX_NEW_POSTS:
        gain = (reach & ~covered).sum(axis=1)
        gain[chosen] = -1
        best = int(gain.argmax())
        if gain[best] < BLACKSPOT_MIN_POST_GAIN:
            break
        chosen.append(best)
        gains.append(int(gain[best]))
        covered |= reach[best]
    if not chosen:
        return empty

    sel = np.array(chosen)
    k = len(sel)
    new_rows = []
    for n, c in enumerate(sel):
        ci = picked[c]
        new_rows.append({
            "site_id": f"{R.reg}_p{n + 1:02d}",
            "h3_index": h3s[ci],
            "site_type": "proposed_post",
            "name": f"Proposed post {n + 1} ({R.cells['zone'].iat[ci]})",
            "lat": float(R.cells["lat"].iat[ci]),
            "lon": float(R.cells["lon"].iat[ci]),
            "node_idx": int(cand_nodes[c]),
        })
    sites_x = pd.concat([R.sites, pd.DataFrame(new_rows)], ignore_index=True)
    fwd_x = np.vstack([R.fwd, out[sel, :nc]])
    back_x = np.hstack([R.back, inn[sel, :nc].T])
    ss_x = np.block([
        [R.ss, inn[sel, nc:nc + ns].T],
        [out[sel, nc:nc + ns], out[np.ix_(sel, nc + ns + sel)]],
    ]).astype(np.float32)
    posts = [
        {**{k2: v for k2, v in row.items() if k2 != "node_idx"}, "zone": str(R.cells["zone"].iat[picked[c]]),
         "late_calls_in_reach": g, "index": len(R.sites) + n}
        for n, (row, c, g) in enumerate(zip(new_rows, sel, gains, strict=True))
    ]
    print(f"[blackspots] {k} posts chosen from {len(picked)} candidates; they reach {int(covered.sum())} of "
          f"{reach.shape[1]} unreachable late calls (tuning window).")
    return {"sites": sites_x, "fwd": fwd_x, "back": back_x, "ss": ss_x, "posts": posts, "candidates": len(picked)}


# ------------------------------------------------------------------------------------------- evaluation


def kpis(log: pd.DataFrame, st: pd.DataFrame, plans: List[BlockPlan], target: float) -> Dict[str, Any]:
    sim = summarize(log, plans, target)
    black = st["black"]
    return {
        "calls": int(sim.total_calls),
        "within_pct": float(sim.within_target_pct),
        "median_min": float(sim.median_min),
        "p90_min": float(sim.p90_min),
        "late": int(log["late"].sum()),
        "excess_min": round(float(log["excess_min"].sum()), 0),
        "black_spots": int(black.sum()),
        "black_calls": int(st.loc[black, "calls"].sum()),
        "black_late": int(st.loc[black, "late"].sum()),
        "first_help_10m_pct": float(sim.first_help_10m_pct),
        "queued_pct": float(sim.queued_pct),
    }


def deployments(plans: List[BlockPlan]) -> List[List[List[int]]]:
    """Per block: [site_index, als, bls, bikes] for every staffed site."""
    out = []
    for p in plans:
        tot = p.a + p.b + p.y
        out.append([[int(s), int(p.a[s]), int(p.b[s]), int(p.y[s])] for s in np.flatnonzero(tot)])
    return out


def fleet_of(plans: List[BlockPlan]) -> Dict[str, int]:
    p = plans[0]
    return {"ambulances": int(p.a.sum() + p.b.sum()), "als": int(p.a.sum()), "bls": int(p.b.sum()),
            "bikes": int(p.y.sum())}


def scenario_payload(key: str, label: str, description: str, plans, log, st, R: Region,
                     ref_black: Optional[np.ndarray], st_before: pd.DataFrame,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One plan's evaluation-window results. st_before: the same plan's stats on the window before."""
    target = R.cfg.target_min
    has = st["calls"] > 0
    code = {c: i for i, c in enumerate(CAUSES)}
    sev = {s: i for i, s in enumerate(SEVERITIES)}
    before = st_before["black"].to_numpy()
    # [cell, calls, late, minutes late, p90, dominant cause, severity, Wilson lower bound of the late share,
    #  late calls out of reach, no unit planned nearby, nearby unit busy]
    cells = [
        [int(i), int(r.calls), int(r.late), round(float(r.excess_min), 1), round(float(r.p90_min), 1),
         code.get(r.cause, -1), sev.get(r.severity, -1), round(float(r.late_lo), 3),
         int(r.n_unreachable), int(r.n_uncovered), int(r.n_busy)]
        for i, r in st[has].iterrows()
    ]
    black = st["black"].to_numpy()
    out = {
        "key": key,
        "label": label,
        "description": description,
        "fleet": fleet_of(plans),
        "kpis": kpis(log, st, plans, target),
        "late_by_cause": {c: int((log["cause"] == c).sum()) for c in CAUSES},
        "cells": cells,
        "clusters": cluster_black_spots(st, R.cells),
        "deployments": deployments(plans),
        # Same plan, previous window: chronic = black in both, new = now only, resolved = before only
        "before_black": [int(i) for i in np.flatnonzero(before)],
        "vs_before": {
            "chronic": int((black & before).sum()),
            "new": int((black & ~before).sum()),
            "resolved": int((~black & before).sum()),
        },
    }
    if ref_black is not None:
        out["vs_current"] = {
            "fixed": int((ref_black & ~black).sum()),
            "remaining": int((ref_black & black).sum()),
            "new": int((~ref_black & black).sum()),
        }
    if extra:
        out.update(extra)
    return out


def window_info(log: pd.DataFrame) -> Dict[str, Any]:
    ts = pd.to_datetime(log["timestamp"])
    return {"start": ts.min().strftime("%Y-%m-%d"), "end": ts.max().strftime("%Y-%m-%d"), "calls": int(len(log))}


# ------------------------------------------------------------------------------------------- plan cache

CACHE_DIR = PROCESSED_DIR / "blackspots_cache"


def solve_cached(reg: str, use_cache: bool, **kw) -> List[BlockPlan]:
    """solve_goldenhour_rolling, cached on disk by its inputs: re-exporting needs no new MILP solves.
    HiGHS stops at time limits, so a fresh solve of the same problem can differ slightly; the cache keeps
    the exported results reproducible."""
    import hashlib
    import pickle

    h = hashlib.sha1(reg.encode())
    for k in sorted(kw):
        v = kw[k]
        if isinstance(v, np.ndarray):
            h.update(k.encode() + np.ascontiguousarray(v).tobytes())
        elif isinstance(v, pd.DataFrame):
            h.update(k.encode() + pd.util.hash_pandas_object(v, index=False).values.tobytes())
        else:
            h.update(f"{k}={v!r}".encode())
    path = CACHE_DIR / f"{reg}_{h.hexdigest()[:16]}.pkl"
    if use_cache and path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    plans = solve_goldenhour_rolling(reg, **kw)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(plans, f)
    return plans


# ---------------------------------------------------------------------------------------------- run


def run_region(reg: str, quick: bool, graph, use_cache: bool = True) -> Dict[str, Any]:
    t_start = time.time()
    R = Region(reg)
    cfg = R.cfg
    n_cells = len(R.cells)
    scale = 0.25 if quick else 1.0
    rounds = 1 if quick else BLACKSPOT_ROUNDS
    fleet_steps = BLACKSPOT_FLEET_STEPS[-1:] if quick else BLACKSPOT_FLEET_STEPS
    print(f"\n{'=' * 70}\nBlack spots: {cfg.name} (target {cfg.target_min:g} min){' [quick]' if quick else ''}\n{'=' * 70}")

    def evaluate(plans, **travel) -> Dict[str, Any]:
        lt = R.replay(plans, "tune", **travel)
        le = R.replay(plans, "test", **travel)
        return {"plans": plans, "log_tune": lt, "st_tune": cell_stats(lt, n_cells, cfg.target_min),
                "log": le, "st": cell_stats(le, n_cells, cfg.target_min)}

    def k_of(ev: Dict[str, Any], window: str) -> Dict[str, Any]:
        if window == "tune":
            return kpis(ev["log_tune"], ev["st_tune"], ev["plans"], cfg.target_min)
        return kpis(ev["log"], ev["st"], ev["plans"], cfg.target_min)

    # The three dashboard plans, exactly as in scenarios_{r}.json
    ev_today = evaluate(plans_from_scenario(R.scenarios["baseline"], R.sites))
    ev_static = evaluate(plans_from_scenario(R.scenarios["static"], R.sites))
    ev_cur = evaluate(plans_from_scenario(R.scenarios["goldenhour"], R.sites))
    ref_black = ev_cur["st"]["black"].to_numpy()
    print(f"[blackspots] GoldenHour plan: {int(ref_black.sum())} black spots, "
          f"{k_of(ev_cur, 'test')['within_pct']}% in time (evaluation window)")

    # Step 1: targeted re-placement, multiplicative weights from each round's tuning-window replay
    weights = np.ones(n_cells)
    iterations = [{"round": 0, "label": "GoldenHour plan", "tune": k_of(ev_cur, "tune"), "test": k_of(ev_cur, "test")}]
    best = None
    st_prev = ev_cur["st_tune"]
    for k in range(1, rounds + 1):
        weights = np.clip(weights * (1.0 + BLACKSPOT_ALPHA * st_prev["late_lo"].to_numpy()), 1.0, BLACKSPOT_MAX_WEIGHT)
        print(f"[blackspots] Round {k}: re-planning with black-spot weights (max {weights.max():.1f})...")
        ev = evaluate(solve_cached(reg, use_cache, lam_multiplier=weights, time_limit_scale=scale))
        it = {"round": k, "label": f"Round {k}", "tune": k_of(ev, "tune"), "test": k_of(ev, "test"),
              "max_weight": round(float(weights.max()), 2)}
        iterations.append(it)
        # Pick on the tuning window only (fewest minutes late in total), never on the evaluation window
        if best is None or it["tune"]["excess_min"] < best["it"]["tune"]["excess_min"]:
            best = {"it": it, "ev": ev, "weights": weights.copy()}
        st_prev = ev["st_tune"]
    best["it"]["selected"] = True
    ev_tgt, w_tgt = best["ev"], best["weights"]

    # Step 2: new standby posts at unreachable black spots (from the targeted plan's tuning replay)
    px = propose_posts(R, ev_tgt["st_tune"], ev_tgt["log_tune"], graph)
    travel_x = {"sites": px["sites"], "fwd": px["fwd"], "back": px["back"], "ss": px["ss"]}
    if px["posts"]:
        print(f"[blackspots] Re-planning with {len(px['posts'])} proposed posts...")
        ev_posts = evaluate(solve_cached(reg, use_cache, lam_multiplier=w_tgt, fwd=px["fwd"], df_sites=px["sites"],
                                                     time_limit_scale=scale), **travel_x)
    else:
        ev_posts = ev_tgt
    n_sites = len(R.sites)

    def posts_used(plans) -> List[int]:
        return sorted({int(s) + n_sites for p in plans for s in np.flatnonzero((p.a + p.b + p.y)[n_sites:])})

    # Step 3: extra ambulances on top (physical fleet; ~74% of it is on the road), one scenario per step
    curve = []
    fleet_evs = []
    for step in fleet_steps:
        physical = int(round(cfg.fleet_ambulances * (1.0 + step)))
        print(f"[blackspots] Re-planning with {physical} ambulances (+{step:.0%})...")
        ev_fleet = evaluate(solve_cached(reg, use_cache, fleet_ambulances=physical, lam_multiplier=w_tgt,
                                                     fwd=px["fwd"], df_sites=px["sites"], time_limit_scale=scale),
                            **travel_x)
        fleet_evs.append((step, physical, ev_fleet))
        k_f = k_of(ev_fleet, "test")
        curve.append({"extra_pct": round(step * 100), "physical": physical,
                      "on_road": fleet_of(ev_fleet["plans"])["ambulances"],
                      **{x: k_f[x] for x in ("within_pct", "black_spots", "late", "p90_min")}})

    def pay(key, label, desc, ev, ref=ref_black, extra=None):
        return scenario_payload(key, label, desc, ev["plans"], ev["log"], ev["st"], R, ref, ev["st_tune"], extra)

    sites_all = px["sites"]
    t = cfg.target_min
    n_posts = len(px["posts"])
    scen = [
        pay("today", "Today", "Ambulances at hospitals, shared by population, the same all day (the CAG-audit "
            "pattern).", ev_today),
        pay("static", "Best fixed spots", "One optimised set of standby spots for the whole day, no bikes.", ev_static),
        pay("current", "GoldenHour plan", "Spots change every 4 hours with demand and traffic; bikes give first help.",
            ev_cur, ref=None),
        pay("targeted", "Re-placed for black spots", f"Same fleet and sites, re-planned with black-spot hexagons "
            f"weighted up (best of {rounds} feedback round(s), picked on the planning weeks).", ev_tgt),
        pay("posts", f"+ {n_posts} new post{'s' if n_posts != 1 else ''}",
            f"Same fleet, plus standby posts at out-of-reach black spots; the plan staffs "
            f"{len(posts_used(ev_posts['plans']))} of {n_posts}.", ev_posts,
            extra={"posts_used": posts_used(ev_posts["plans"])}),
    ]
    for step, physical, ev_f in fleet_evs:
        extra = physical - cfg.fleet_ambulances
        pct = round(step * 100)
        scen.append(pay(f"fleet{pct}", f"+ {extra} ambulances (+{pct}%)",
                        f"New posts plus {extra} more ambulances ({physical} in the fleet, ~74% on the road).", ev_f,
                        extra={"posts_used": posts_used(ev_f["plans"])}))

    # Before/after for every stage: each stage against the one before it (GoldenHour -> 1 -> 2 -> 3a/3b)
    by_key = {x["key"]: x for x in scen}
    prev_of = {"targeted": "current", "posts": "targeted"}
    prev_of.update({x["key"]: "posts" for x in scen if x["key"].startswith("fleet")})
    stages = []
    for key, prev in [("current", None)] + list(prev_of.items()):
        x = by_key[key]
        b_now = {r[0] for r in x["cells"] if r[6] >= 0}
        row = {"key": key, "label": x["label"], "prev": prev, "kpis": {k: x["kpis"][k] for k in (
            "black_spots", "black_late", "late", "within_pct", "p90_min", "median_min")},
            "late_by_cause": x["late_by_cause"], "fleet": x["fleet"]}
        if prev:
            b_prev = {r[0] for r in by_key[prev]["cells"] if r[6] >= 0}
            row["vs_prev"] = {"resolved": len(b_prev - b_now), "remaining": len(b_prev & b_now), "new": len(b_now - b_prev)}
        stages.append(row)

    payload = {
        "region": reg,
        "region_name": cfg.name,
        "zone_label": cfg.zone_label,
        "target_min": t,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quick": quick,
        "synthetic": True,
        "windows": {"tune": window_info(ev_cur["log_tune"]), "test": window_info(ev_cur["log"]), "days": BLACKSPOT_WINDOW_DAYS},
        "criteria": {
            "min_late": BLACKSPOT_MIN_LATE,
            "min_late_share": BLACKSPOT_MIN_LATE_SHARE,
            "confidence": "one-sided 95% (Wilson lower bound, z=1.645)",
            "alpha": BLACKSPOT_ALPHA,
            "max_weight": BLACKSPOT_MAX_WEIGHT,
            "rounds": rounds,
            "max_new_posts": BLACKSPOT_MAX_NEW_POSTS,
            "min_post_gain": BLACKSPOT_MIN_POST_GAIN,
            "candidate_posts": px["candidates"],
        },
        "causes": [{"key": c, **CAUSE_INFO[c]} for c in CAUSES],
        "n_sites": n_sites,
        "posts": px["posts"],
        "scenarios": scen,
        "iterations": iterations,
        "fleet_curve": curve,
        "stages": stages,
        "solver_seconds": round(time.time() - t_start, 1),
    }
    assert len(sites_all) == n_sites + n_posts
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Find ambulance black spots and re-plan placement to fix them.")
    ap.add_argument("--region", choices=list(REGIONS), action="append", help="default: all regions")
    ap.add_argument("--quick", action="store_true", help="short solver limits, one round (for demos and tests)")
    ap.add_argument("--no-cache", action="store_true", help="re-solve every plan instead of reusing cached ones")
    args = ap.parse_args()

    t0 = time.time()
    graph = load_graph()
    for reg in args.region or list(REGIONS):
        payload = run_region(reg, args.quick, graph, use_cache=not args.no_cache)
        out = WEB_DATA_DIR / f"blackspots_{reg}.json"
        out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        cur = next(s for s in payload["scenarios"] if s["key"] == "current")["kpis"]
        print(f"[blackspots] {reg}: {cur['black_spots']} black spots now ->",
              ", ".join(f"{s['key']} {s['kpis']['black_spots']}" for s in payload["scenarios"]),
              f"| wrote {out.relative_to(ROOT_PATH)} ({out.stat().st_size / 1e3:.0f} kB)")
    print(f"[blackspots] Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
