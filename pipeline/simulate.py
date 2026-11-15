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


HOUR_TO_BLOCK = {h: b["id"] for b in PLANNING_BLOCKS for h in b["hours"]}


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


def replay_calls(
    reg_code: str,
    plans: List[BlockPlan],
    df_calls: pd.DataFrame,
    df_cells: pd.DataFrame,
    df_sites: pd.DataFrame,
    fwd: np.ndarray,
    back: np.ndarray,
    ss: np.ndarray,
    sim_days: int = SIMULATION_DAYS,
    end_offset_days: int = 0,
) -> pd.DataFrame:
    """Replay sim_days of calls ending end_offset_days before the last day; one row per call.

    Besides the response time, each row records two counterfactual drive times that explain a late arrival:
      planned_min: call handling + drive from the nearest standby site the plan staffs in that block
                   (what the caller would get if every planned unit were home and free)
      best_min:    call handling + drive from the nearest candidate site of any kind
                   (the best any placement of this candidate set could do)
    Sites may include proposed posts beyond df_sites (fwd/ss rows past len(df_sites)); they are never hospitals.
    """
    cfg = REGIONS[reg_code]
    rng = np.random.default_rng(SEED)
    n_bikes = int(plans[0].y.sum())

    h3_to_idx = {h: i for i, h in enumerate(df_cells["h3_index"])}

    # Hospital sites for patient transport
    hosp_sites = np.where(df_sites["site_type"].to_numpy() == "hospital")[0]
    if len(hosp_sites) == 0:
        hosp_sites = np.arange(len(df_sites))
    cell_nearest_hosp = hosp_sites[np.argmin(back[:, hosp_sites], axis=1)]

    # Daily vehicle standby chain
    vehicle_standbys, amb_is_als = link_vehicle_standbys(plans, ss)
    n_amb = len(amb_is_als)

    # Calls in the replay window
    max_day = int(df_calls["day_of_year"].max()) - end_offset_days
    min_day = max_day - sim_days + 1
    in_window = (df_calls["day_of_year"] >= min_day) & (df_calls["day_of_year"] <= max_day)
    test_calls = df_calls[in_window].sort_values("timestamp").reset_index(drop=True)
    n_calls = len(test_calls)

    ts = pd.to_datetime(test_calls["timestamp"])
    call_times_sec = (ts - ts.iloc[0]).dt.total_seconds().to_numpy() if n_calls else np.zeros(0)
    hours = test_calls["hour"].to_numpy(dtype=np.int64)
    blocks = test_calls["block"].to_numpy(dtype=np.int64)
    cell_idx = np.array([h3_to_idx.get(h, 0) for h in test_calls["h3_index"]], dtype=np.int64)
    needs_als_arr = test_calls["needs_als"].to_numpy(dtype=bool)
    bike_helps_arr = test_calls["bike_helps"].to_numpy(dtype=bool)

    # Vehicle state
    amb_free_time = np.zeros(n_amb, dtype=np.float64)
    amb_curr_site = vehicle_standbys[:, 0].copy() if n_amb else np.zeros(0, dtype=np.int32)
    # Time each unit is back at its standby site; until then it sits at the hospital it just left
    amb_home_time = np.zeros(n_amb, dtype=np.float64)

    # Bikes follow the plan block by block too (chained like the ambulances)
    bike_free_time = np.zeros(n_bikes, dtype=np.float64)
    bike_standbys = _chain([p.y for p in plans], ss)

    hourly_factors = list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24

    # Counterfactual free-flow seconds per cell: nearest staffed site per block, nearest candidate site overall
    planned_ff = np.stack([
        fwd[np.unique(vehicle_standbys[:, b])].min(axis=0) if n_amb else np.full(fwd.shape[1], np.inf)
        for b in range(len(plans))
    ])
    best_ff = fwd.min(axis=0)

    response_times = np.zeros(n_calls, dtype=np.float32)
    first_help_times = np.zeros(n_calls, dtype=np.float32)
    was_queued = np.zeros(n_calls, dtype=bool)
    als_received = np.zeros(n_calls, dtype=bool)
    unit_from = np.zeros(n_calls, dtype=np.int32)
    planned_min = np.zeros(n_calls, dtype=np.float32)
    best_min = np.zeros(n_calls, dtype=np.float32)

    for k in range(n_calls):
        t_call = call_times_sec[k]
        hour = hours[k]
        b_id = blocks[k]
        c_idx = cell_idx[k]
        needs_als = needs_als_arr[k]

        factor = hourly_factors[hour]
        amb_speed_fact = factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)  # minutes per second
        planned_min[k] = PRE_TRAVEL_MIN + planned_ff[b_id, c_idx] * amb_speed_fact
        best_min[k] = PRE_TRAVEL_MIN + best_ff[c_idx] * amb_speed_fact

        # 1. Check bike dispatch
        bike_arrival_min = np.inf
        if bike_helps_arr[k] and n_bikes > 0:
            free_bikes = np.where(bike_free_time <= t_call)[0]
            if len(free_bikes) > 0:
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
            # Units that have finished driving back sit at the current block's standby site;
            # units still on the way back are dispatched from the hospital they left (GPS-tracked)
            home = free_ambs[amb_home_time[free_ambs] <= t_call]
            amb_curr_site[home] = vehicle_standbys[home, b_id]

            v_sites = amb_curr_site[free_ambs]
            drive_mins = fwd[v_sites, c_idx] * amb_speed_fact

            # Prioritize ALS if critical
            best_sub = int(np.argmin(drive_mins))
            if needs_als:
                als_free_idx = np.where(amb_is_als[free_ambs])[0]
                if len(als_free_idx) and drive_mins[als_free_idx].min() <= drive_mins[best_sub] + ALS_EXTRA_MIN:
                    best_sub = int(als_free_idx[np.argmin(drive_mins[als_free_idx])])
            chosen_v = free_ambs[best_sub]
            chosen_drive = drive_mins[best_sub]
            queue_wait_min = 0.0
        else:
            # Queue call: wait for earliest free ambulance, which becomes free at its handover hospital
            chosen_v = int(np.argmin(amb_free_time))
            queue_wait_min = (amb_free_time[chosen_v] - t_call) / 60.0
            chosen_drive = fwd[amb_curr_site[chosen_v], c_idx] * amb_speed_fact
            was_queued[k] = True

        unit_from[k] = amb_curr_site[chosen_v]
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
        free_hour = int((hour + total_job_min / 60.0) % 24)
        home_site = vehicle_standbys[chosen_v, HOUR_TO_BLOCK[free_hour]]
        amb_home_time[chosen_v] = amb_free_time[chosen_v] + ss[dest_hosp, home_site] * amb_speed_fact * 60.0

    log = test_calls[["call_id", "timestamp", "day_of_year", "hour", "block", "h3_index", "zone", "call_type",
                      "needs_als", "bike_helps"]].copy()
    log["cell_idx"] = cell_idx
    log["response_min"] = response_times
    log["first_help_min"] = first_help_times
    log["queued"] = was_queued
    log["als_received"] = als_received
    log["unit_from_site"] = unit_from
    log["planned_min"] = planned_min
    log["best_min"] = best_min
    return log


def summarize(log: pd.DataFrame, plans: List[BlockPlan], target_min: float) -> SimulationResults:
    """Aggregate a replay log into the dashboard metrics."""
    n_calls = len(log)
    if n_calls == 0:
        return SimulationResults(0, 0, 0, 0, 0, 0, 0, 0, [0] * 61, [], {})
    response_times = log["response_min"].to_numpy()
    first_help_times = log["first_help_min"].to_numpy()

    within_target = (response_times <= target_min).mean() * 100.0
    crit_mask = log["needs_als"].to_numpy(dtype=bool)
    als_received = log["als_received"].to_numpy(dtype=bool)
    als_pct = (als_received[crit_mask].mean() * 100.0) if np.any(crit_mask) else 100.0

    # 0 to 60 minute response histogram
    hist_counts, _ = np.histogram(response_times, bins=61, range=(0, 61))

    per_block = []
    blocks = log["block"].to_numpy()
    for b_id in range(6):
        b_resp = response_times[blocks == b_id]
        has = len(b_resp) > 0
        per_block.append({
            "block_id": b_id,
            "name": PLANNING_BLOCKS[b_id]["name"],
            "calls": int(len(b_resp)),
            "within_target_pct": round(float((b_resp <= target_min).mean() * 100.0), 1) if has else 0.0,
            "median_min": round(float(np.median(b_resp)), 1) if has else 0.0,
            "p90_min": round(float(np.percentile(b_resp, 90)), 1) if has else 0.0,
            "busy_fraction": round(float(plans[b_id].busy_fraction), 3),
        })

    per_zone = {}
    for z, grp in log.assign(in_target=log["response_min"] <= target_min).groupby("zone"):
        per_zone[z] = {
            "calls": len(grp),
            "within_target_pct": round(float(grp["in_target"].mean() * 100.0), 1),
            "median_min": round(float(grp["response_min"].median()), 1),
            "p90_min": round(float(np.percentile(grp["response_min"], 90)), 1),
        }

    return SimulationResults(
        total_calls=n_calls,
        within_target_pct=round(within_target, 1),
        median_min=round(float(np.median(response_times)), 1),
        p90_min=round(float(np.percentile(response_times, 90)), 1),
        p95_min=round(float(np.percentile(response_times, 95)), 1),
        first_help_10m_pct=round((first_help_times <= 10.0).mean() * 100.0, 1),
        queued_pct=round(log["queued"].mean() * 100.0, 1),
        als_for_critical_pct=round(als_pct, 1),
        histogram=hist_counts.tolist(),
        per_block=per_block,
        per_zone=per_zone,
    )


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
    log = replay_calls(reg_code, plans, df_calls, df_cells, df_sites, fwd, back, ss, sim_days)
    return summarize(log, plans, REGIONS[reg_code].target_min)
