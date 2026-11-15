"""GoldenHour: Discrete-Event Simulation Pipeline for Ambulance & Bike Operations.

Simulates emergency response operations over the last 28 days of calls:
  - Nearest-free ambulance dispatch (C-DAC NG-ERSS command centre protocol)
  - First-responder bike simultaneous dispatch for eligible calls
  - Job cycle: 3m pre-travel dispatch -> drive to scene -> on scene (Gamma 15m) ->
    drive to nearest hospital -> handover (Gamma 20m)
  - Units become FREE at hospital immediately upon handover completion (GPS-tracked)
  - Standby sites across shifts linked by linear assignment
  - Metrics: % within target, median, 90th percentile, first help <= 10m, queued %, ALS coverage.
"""

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from pipeline.config import (
    ALS_EXTRA_MIN,
    AMBULANCE_SPEED_MULTIPLIER,
    BENGALURU_HOURLY_CONGESTION,
    PLANNING_BLOCKS,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    REGIONS,
    SEED,
    SIMULATION_DAYS,
)
from pipeline.optimize import BlockPlan


@dataclass
class SimulationResults:
    total_calls: int
    within_target_pct: float
    median_min: float
    p90_min: float
    p95_min: float
    first_help_10m_pct: float
    queued_pct: float
    als_for_critical_pct: float
    histogram: List[int]  # 0 to 60+ minutes in 1-minute bins
    per_block: List[Dict[str, Any]]
    per_zone: Dict[str, Dict[str, Any]]


def _chain(counts_per_block: List[np.ndarray], ss_matrix: np.ndarray) -> np.ndarray:
    """Give each physical vehicle one standby site per block; consecutive blocks are linked by
    least-total-driving linear assignment, so a vehicle keeps its spot unless the plan moves it."""
    homes = [np.repeat(np.arange(len(c)), c) for c in counts_per_block]
    n = len(homes[0])
    out = np.zeros((n, len(homes)), dtype=np.int32)
    if n == 0:
        return out
    out[:, 0] = homes[0]
    for t in range(1, len(homes)):
        r, c = linear_sum_assignment(ss_matrix[np.ix_(out[:, t - 1], homes[t])])
        out[r, t] = homes[t][c]
    return out


def link_vehicle_standbys(plans: List[BlockPlan], ss_matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Chain Advanced (ALS) and Basic (BLS) units separately, so the simulated ALS units are the ones
    the plan placed as ALS. Returns (standby site per vehicle per block, is_als per vehicle)."""
    als = _chain([p.a for p in plans], ss_matrix)
    bls = _chain([p.b for p in plans], ss_matrix)
    sites = np.vstack([als, bls])
    is_als = np.r_[np.ones(len(als), bool), np.zeros(len(bls), bool)]
    return sites, is_als


def simulate_plan(
    reg_code: str,
    plans: List[BlockPlan],
    df_calls: pd.DataFrame,
    df_cells: pd.DataFrame,
    df_sites: pd.DataFrame,
    fwd: np.ndarray,
    back: np.ndarray,
    ss: np.ndarray,
    sim_days: int = SIMULATION_DAYS,
) -> SimulationResults:
    """Run discrete-event simulation over the last sim_days of calls."""
    cfg = REGIONS[reg_code]
    rng = np.random.default_rng(SEED)

    target_min = cfg.target_min
    n_bikes = int(plans[0].y.sum())

    # Map cells to index 0..N-1
    h3_to_idx = {h: i for i, h in enumerate(df_cells["h3_index"])}
    zones_list = df_cells["zone"].values

    # Hospital sites for patient transport
    hosp_sites = np.where(df_sites["site_type"] == "hospital")[0]
    if len(hosp_sites) == 0:
        hosp_sites = np.arange(len(df_sites))

    # Nearest hospital index for each cell
    back_hosp = back[:, hosp_sites]
    nearest_hosp_local = np.argmin(back_hosp, axis=1)
    cell_nearest_hosp = hosp_sites[nearest_hosp_local]

    # Daily vehicle standby chain
    vehicle_standbys, amb_is_als = link_vehicle_standbys(plans, ss)
    n_amb = len(amb_is_als)

    # Filter calls to last sim_days
    max_day = df_calls["day_of_year"].max()
    min_day = max_day - sim_days + 1
    test_calls = df_calls[df_calls["day_of_year"] >= min_day].copy()
    test_calls = test_calls.sort_values("timestamp").reset_index(drop=True)

    n_calls = len(test_calls)
    if n_calls == 0:
        return SimulationResults(0, 0, 0, 0, 0, 0, 0, 0, [0]*61, [], {})

    # Convert timestamps to simulation seconds from start
    start_ts = pd.to_datetime(test_calls["timestamp"].iloc[0])
    call_times_sec = (
        pd.to_datetime(test_calls["timestamp"]) - start_ts
    ).dt.total_seconds().values

    # Vehicle state:
    # available_time_sec (float)
    # current_site (int)
    # is_als (bool)
    amb_free_time = np.zeros(n_amb, dtype=np.float64)
    amb_curr_site = vehicle_standbys[:, 0].copy()
    # Time each unit is back at its standby site; until then it sits at the hospital it just left
    amb_home_time = np.zeros(n_amb, dtype=np.float64)

    # Bike state
    bike_free_time = np.zeros(n_bikes, dtype=np.float64)
    # Bikes follow the plan block by block too (chained like the ambulances)
    bike_standbys = _chain([p.y for p in plans], ss)

    hourly_factors = (
        list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24
    )

    # Output metric logs
    response_times = np.zeros(n_calls, dtype=np.float32)
    first_help_times = np.zeros(n_calls, dtype=np.float32)
    was_queued = np.zeros(n_calls, dtype=bool)
    als_received = np.zeros(n_calls, dtype=bool)

    # Simulation main loop
    for k in range(n_calls):
        t_call = call_times_sec[k]
        hour = test_calls["hour"].iloc[k]
        b_id = test_calls["block"].iloc[k]
        c_h3 = test_calls["h3_index"].iloc[k]
        c_idx = h3_to_idx.get(c_h3, 0)
        needs_als = test_calls["needs_als"].iloc[k]
        bike_helps = test_calls["bike_helps"].iloc[k]

        factor = hourly_factors[hour]
        amb_speed_fact = factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)  # minutes per second

        # 1. Check bike dispatch
        bike_arrival_min = np.inf
        if bike_helps and n_bikes > 0:
            free_bikes = np.where(bike_free_time <= t_call)[0]
            if len(free_bikes) > 0:
                # Drive times from current bike sites
                b_sites = bike_standbys[free_bikes, b_id]
                b_drive_mins = fwd[b_sites, c_idx] * (1.0 + 0.4 * (factor - 1.0)) / 60.0
                best_b_local = np.argmin(b_drive_mins)
                best_b_idx = free_bikes[best_b_local]
                b_drive = b_drive_mins[best_b_local]

                if b_drive <= 15.0:
                    bike_arrival_min = PRE_TRAVEL_MIN + b_drive
                    # Bike service time: on scene (mean 15m) + return
                    b_on_scene = rng.gamma(shape=15.0, scale=1.0)
                    bike_free_time[best_b_idx] = t_call + (PRE_TRAVEL_MIN + b_drive + b_on_scene + b_drive) * 60.0

        # 2. Dispatch ambulance
        free_ambs = np.where(amb_free_time <= t_call)[0]
        if len(free_ambs) > 0:
            # Idle units: update location to scheduled standby for block b_id
            # Units that have finished driving back sit at the current block's standby site;
            # units still on the way back are dispatched from the hospital they left (GPS-tracked)
            for v in free_ambs:
                if amb_home_time[v] <= t_call:
                    amb_curr_site[v] = vehicle_standbys[v, b_id]

            # Compute drive times from vehicle current sites
            v_sites = amb_curr_site[free_ambs]
            drive_mins = fwd[v_sites, c_idx] * amb_speed_fact

            # Prioritize ALS if critical
            if needs_als:
                als_mask = amb_is_als[free_ambs]
                als_free_idx = np.where(als_mask)[0]
                best_any = int(np.argmin(drive_mins))
                if len(als_free_idx) and drive_mins[als_free_idx].min() <= drive_mins[best_any] + ALS_EXTRA_MIN:
                    best_als_sub = np.argmin(drive_mins[als_free_idx])
                    chosen_v = free_ambs[als_free_idx[best_als_sub]]
                    chosen_drive = drive_mins[als_free_idx[best_als_sub]]
                else:
                    best_sub = np.argmin(drive_mins)
                    chosen_v = free_ambs[best_sub]
                    chosen_drive = drive_mins[best_sub]
            else:
                best_sub = np.argmin(drive_mins)
                chosen_v = free_ambs[best_sub]
                chosen_drive = drive_mins[best_sub]

            queue_wait_min = 0.0
            was_queued[k] = False
        else:
            # Queue call: wait for earliest free ambulance
            earliest_v = np.argmin(amb_free_time)
            queue_wait_min = (amb_free_time[earliest_v] - t_call) / 60.0
            chosen_v = earliest_v
            # Vehicle becomes free at its handover hospital
            hosp_s = amb_curr_site[chosen_v]
            chosen_drive = fwd[hosp_s, c_idx] * amb_speed_fact
            was_queued[k] = True

        # Total response time
        resp_min = queue_wait_min + PRE_TRAVEL_MIN + chosen_drive
        response_times[k] = resp_min
        first_help_times[k] = min(resp_min, bike_arrival_min)
        als_received[k] = bool(amb_is_als[chosen_v])

        # Ambulance cycle: on scene + transport + handover
        on_scene_min = rng.gamma(shape=15.0, scale=1.0)
        dest_hosp = cell_nearest_hosp[c_idx]
        transp_min = back[c_idx, dest_hosp] * amb_speed_fact
        handover_min = rng.gamma(shape=20.0, scale=1.0)

        total_job_min = resp_min + on_scene_min + transp_min + handover_min
        # Unit becomes FREE at hospital immediately at handover finish
        amb_free_time[chosen_v] = t_call + (total_job_min * 60.0)
        amb_curr_site[chosen_v] = dest_hosp
        # then drives back to its standby site for the block it is in at that moment
        free_hour = int((test_calls["hour"].iloc[k] + total_job_min / 60.0) % 24)
        home_site = vehicle_standbys[chosen_v, next(b["id"] for b in PLANNING_BLOCKS if free_hour in b["hours"])]
        amb_home_time[chosen_v] = amb_free_time[chosen_v] + ss[dest_hosp, home_site] * amb_speed_fact * 60.0

    # Aggregate metrics
    within_target = (response_times <= target_min).mean() * 100.0
    med_resp = float(np.median(response_times))
    p90_resp = float(np.percentile(response_times, 90))
    p95_resp = float(np.percentile(response_times, 95))
    first_help_10m = (first_help_times <= 10.0).mean() * 100.0
    queued_pct = was_queued.mean() * 100.0

    crit_mask = test_calls["needs_als"].values
    als_pct = (
        (als_received[crit_mask].mean() * 100.0) if np.any(crit_mask) else 100.0
    )

    # 0 to 60 minute response histogram
    hist_counts, _ = np.histogram(response_times, bins=61, range=(0, 61))
    hist_list = hist_counts.tolist()

    # Per-block summaries
    per_block = []
    for b_id in range(6):
        b_mask = test_calls["block"].values == b_id
        if np.any(b_mask):
            b_resp = response_times[b_mask]
            per_block.append({
                "block_id": b_id,
                "name": PLANNING_BLOCKS[b_id]["name"],
                "calls": int(b_mask.sum()),
                "within_target_pct": round(float((b_resp <= target_min).mean() * 100.0), 1),
                "median_min": round(float(np.median(b_resp)), 1),
                "p90_min": round(float(np.percentile(b_resp, 90)), 1),
                "busy_fraction": round(float(plans[b_id].busy_fraction), 3),
            })
        else:
            per_block.append({
                "block_id": b_id,
                "name": PLANNING_BLOCKS[b_id]["name"],
                "calls": 0,
                "within_target_pct": 0.0,
                "median_min": 0.0,
                "p90_min": 0.0,
                "busy_fraction": round(float(plans[b_id].busy_fraction), 3),
            })

    # Per-zone statistics
    test_calls["response_min"] = response_times
    test_calls["in_target"] = response_times <= target_min
    per_zone = {}
    for z, grp in test_calls.groupby("zone"):
        per_zone[z] = {
            "calls": len(grp),
            "within_target_pct": round(float(grp["in_target"].mean() * 100.0), 1),
            "median_min": round(float(grp["response_min"].median()), 1),
            "p90_min": round(float(np.percentile(grp["response_min"], 90)), 1),
        }

    return SimulationResults(
        total_calls=n_calls,
        within_target_pct=round(within_target, 1),
        median_min=round(med_resp, 1),
        p90_min=round(p90_resp, 1),
        p95_min=round(p95_resp, 1),
        first_help_10m_pct=round(first_help_10m, 1),
        queued_pct=round(queued_pct, 1),
        als_for_critical_pct=round(als_pct, 1),
        histogram=hist_list,
        per_block=per_block,
        per_zone=per_zone,
    )
