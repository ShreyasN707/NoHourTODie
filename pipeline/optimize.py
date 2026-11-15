"""GoldenHour: Ambulance & Bike Deployment Optimizer using MEXCLP with HiGHS.

Formulation:
  - Multi-period Maximum Expected Covering Location Problem (MEXCLP, Daskin 1983)
  - Solved as a Mixed-Integer Linear Program (MILP) using open-source HiGHS via scipy.optimize.milp
  - Rolling horizon decomposition across six 4-hour planning blocks
  - Graded coverage partial credit (0.4 at 1.5x target, 0.2 at 2.0x target) to minimize extreme delays
  - Soft equity floor ensuring minimum coverage across every administrative ward/taluk
  - Crew reassignment smoothing capped at 25% fleet moves between shifts, solved via Linear Assignment (SciPy)
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
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, linear_sum_assignment, milp

from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    BASE_AVAILABILITY,
    BENGALURU_HOURLY_CONGESTION,
    CRITICAL_CALL_SHARE,
    BIKE_ELIGIBLE_CALL_SHARE,
    MAX_AMBULANCES_PER_SITE,
    MAX_BIKES_PER_SITE,
    MEXCLP_COVER_LEVELS,
    MOVES_CAP_FRACTION,
    NEAREST_CANDIDATE_SITES,
    PLANNING_BLOCKS,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    REGIONS,
    WARD_MIN_COVER_FLOOR,
)


def on_road(n_vehicles: int, availability: float = BASE_AVAILABILITY) -> int:
    """Vehicles actually on the road: ~74% of the 108 fleet is available (◐ news reports, 2025)."""
    return max(1, int(round(n_vehicles * availability))) if n_vehicles > 0 else 0


@dataclass
class BlockPlan:
    block_id: int
    a: np.ndarray  # ALS count per site
    b: np.ndarray  # BLS count per site
    y: np.ndarray  # Bike count per site
    q: float
    busy_fraction: float
    expected_cover: np.ndarray  # Analytic coverage per cell [0, 1]
    nearest_min: np.ndarray     # Nearest ambulance drive time (min) per cell
    bike_cover: np.ndarray      # Bike coverage per cell [0, 1]
    objective_value: float = 0.0


def compute_busy_fraction(
    lam: np.ndarray,
    drive_min: np.ndarray,
    n_ambulances: int,
) -> float:
    """Calculate expected vehicle busy fraction q per MEXCLP queueing approximation."""
    tot_lam = float(lam.sum())
    if tot_lam <= 0 or n_ambulances <= 0:
        return 0.05

    nearest_drive = drive_min.min(axis=0)
    mean_drive = float((nearest_drive * lam).sum() / tot_lam)
    # job_minutes = on_scene (15m) + handover (20m) + 3 x drive_time
    job_min = 15.0 + 20.0 + 3.0 * mean_drive
    q = (tot_lam * job_min) / (60.0 * n_ambulances)
    return float(np.clip(q, 0.05, 0.90))


def solve_single_mexclp(
    reg_code: str,
    block_id: int,
    lam: np.ndarray,
    drive_min: np.ndarray,
    bike_drive_min: np.ndarray,
    df_cells: pd.DataFrame,
    df_sites: pd.DataFrame,
    fleet_ambulances: int,
    als_share: float,
    fleet_bikes: int,
    prev_tot_plan: Optional[np.ndarray] = None,
    next_tot_plan: Optional[np.ndarray] = None,
    move_cap: Optional[int] = None,
    time_limit_sec: float = 30.0,
) -> BlockPlan:
    """Solve single-block MEXCLP MILP with graded coverage, equity floor, and move constraints."""
    cfg = REGIONS[reg_code]
    n_sites = len(df_sites)
    n_cells = len(df_cells)
    tot_lam = float(lam.sum())

    target_min = cfg.target_min
    t_target_drive = target_min - PRE_TRAVEL_MIN
    t_15_drive = 1.5 * target_min - PRE_TRAVEL_MIN
    t_20_drive = 2.0 * target_min - PRE_TRAVEL_MIN
    t_bike_drive = 10.0 - PRE_TRAVEL_MIN

    q = compute_busy_fraction(lam, drive_min, fleet_ambulances)

    # Weights
    w1 = (1.0 - q)
    w2 = (1.0 - q) * q
    w3 = (1.0 - q) * (q ** 2)
    w_15 = 0.40 * (1.0 - q)
    w_20 = 0.20 * (1.0 - q)
    w_als = 1.0 * CRITICAL_CALL_SHARE * (1.0 - q)
    w_bike = 0.5 * BIKE_ELIGIBLE_CALL_SHARE * (1.0 - q)

    # Coverage candidate adjacency
    # To keep solver fast, restrict each cell to NEAREST_CANDIDATE_SITES
    nearest_site_indices = np.argsort(drive_min, axis=0)[:NEAREST_CANDIDATE_SITES, :]  # (25, n_cells)

    # Build sparse coverage matrices (n_cells, n_sites)
    row_std, col_std = [], []
    row_15, col_15 = [], []
    row_20, col_20 = [], []
    row_bike, col_bike = [], []

    for i in range(n_cells):
        sites_for_i = nearest_site_indices[:, i]
        for s in sites_for_i:
            d = drive_min[s, i]
            if d <= t_target_drive:
                row_std.append(i)
                col_std.append(s)
            elif d <= t_15_drive:
                row_15.append(i)
                col_15.append(s)
            elif d <= t_20_drive:
                row_20.append(i)
                col_20.append(s)

        # Bike cover (from all sites within bike target)
        bike_sites = np.where(bike_drive_min[:, i] <= t_bike_drive)[0]
        for s in bike_sites:
            row_bike.append(i)
            col_bike.append(s)

    A_cov_std = sp.csr_matrix((np.ones(len(row_std), dtype=np.float32), (row_std, col_std)), shape=(n_cells, n_sites))
    A_cov_15 = sp.csr_matrix((np.ones(len(row_15), dtype=np.float32), (row_15, col_15)), shape=(n_cells, n_sites))
    A_cov_20 = sp.csr_matrix((np.ones(len(row_20), dtype=np.float32), (row_20, col_20)), shape=(n_cells, n_sites))
    A_cov_bike = sp.csr_matrix((np.ones(len(row_bike), dtype=np.float32), (row_bike, col_bike)), shape=(n_cells, n_sites))

    # Wards for equity soft floor
    unique_wards = sorted(df_cells["zone"].unique())
    n_wards = len(unique_wards)
    ward_to_idx = {w: i for i, w in enumerate(unique_wards)}
    ward_cell_counts = df_cells["zone"].value_counts().to_dict()
    ward_tot_lam = df_cells.groupby("zone")["pop"].sum().to_dict()  # proxy weight

    # Variables:
    # 0 : n_sites                -> a (ALS count, int [0, 3])
    # n_sites : 2*n_sites        -> b (BLS count, int [0, 3])
    # 2*n_sites : 3*n_sites      -> y (Bike count, int [0, 2])
    # 3*n_sites : 3*n_sites + n_cells      -> z1 (cont [0, 1])
    # + n_cells : + 2*n_cells    -> z2 (cont [0, 1])
    # + 2*n_cells : + 3*n_cells  -> z3 (cont [0, 1])
    # + 3*n_cells : + 4*n_cells  -> z15 (cont [0, 1])
    # + 4*n_cells : + 5*n_cells  -> z20 (cont [0, 1])
    # + 5*n_cells : + 6*n_cells  -> za (cont [0, 1])
    # + 6*n_cells : + 7*n_cells  -> u (bike cover, cont [0, 1])
    # + 7*n_cells : + 7*n_cells + n_wards  -> s_w (ward slack, cont >= 0)
    # + n_wards : + n_wards + n_sites      -> r_j (moves from prev, cont >= 0)
    # + n_sites : + n_sites + n_sites      -> r_next_j (moves to next, cont >= 0, if next_tot_plan)

    has_prev = prev_tot_plan is not None and move_cap is not None
    has_next = next_tot_plan is not None and move_cap is not None

    idx_a = 0
    idx_b = n_sites
    idx_y = 2 * n_sites
    idx_z1 = 3 * n_sites
    idx_z2 = idx_z1 + n_cells
    idx_z3 = idx_z2 + n_cells
    idx_z15 = idx_z3 + n_cells
    idx_z20 = idx_z15 + n_cells
    idx_za = idx_z20 + n_cells
    idx_u = idx_za + n_cells
    idx_sw = idx_u + n_cells

    curr_idx = idx_sw + n_wards
    if has_prev:
        idx_r_prev = curr_idx
        curr_idx += n_sites
    else:
        idx_r_prev = None

    if has_next:
        idx_r_next = curr_idx
        curr_idx += n_sites
    else:
        idx_r_next = None

    n_vars = curr_idx

    # Objective coefficients (minimize -profit)
    c = np.zeros(n_vars, dtype=np.float64)
    # Coverage gains
    c[idx_z1 : idx_z1 + n_cells] = -lam * w1
    c[idx_z2 : idx_z2 + n_cells] = -lam * w2
    c[idx_z3 : idx_z3 + n_cells] = -lam * w3
    c[idx_z15 : idx_z15 + n_cells] = -lam * w_15
    c[idx_z20 : idx_z20 + n_cells] = -lam * w_20
    c[idx_za : idx_za + n_cells] = -lam * w_als
    c[idx_u : idx_u + n_cells] = -lam * w_bike

    # Equity penalty: a ward's shortfall costs 5x its own expected calls, so it outweighs any coverage
    # gain elsewhere but a near-empty ward cannot pull the whole fleet towards it
    zone_idx = df_cells["zone"].map(ward_to_idx).values
    ward_lam = np.bincount(zone_idx, weights=lam, minlength=n_wards)
    c[idx_sw : idx_sw + n_wards] = 5.0 * ward_lam

    # Move penalties (0.01 per move to avoid gratuitous swaps)
    if has_prev:
        c[idx_r_prev : idx_r_prev + n_sites] = 0.01
    if has_next:
        c[idx_r_next : idx_r_next + n_sites] = 0.01

    # Variable bounds and integrality
    lb = np.zeros(n_vars, dtype=np.float64)
    ub = np.full(n_vars, np.inf, dtype=np.float64)
    integrality = np.zeros(n_vars, dtype=np.int32)

    # a, b in [0, 3] (integer)
    ub[idx_a : idx_a + n_sites] = MAX_AMBULANCES_PER_SITE
    integrality[idx_a : idx_a + n_sites] = 1

    ub[idx_b : idx_b + n_sites] = MAX_AMBULANCES_PER_SITE
    integrality[idx_b : idx_b + n_sites] = 1

    # y (bikes) in [0, 2] (integer)
    ub[idx_y : idx_y + n_sites] = MAX_BIKES_PER_SITE if fleet_bikes > 0 else 0
    integrality[idx_y : idx_y + n_sites] = 1

    # Coverage variables in [0, 1]
    ub[idx_z1 : idx_sw] = 1.0

    # Constraints list
    A_rows = []
    rhs_lb = []
    rhs_ub = []

    # 1. Total ambulances: sum(a + b) == fleet_ambulances
    row = sp.dok_matrix((1, n_vars), dtype=np.float64)
    for j in range(n_sites):
        row[0, idx_a + j] = 1.0
        row[0, idx_b + j] = 1.0
    A_rows.append(row.tocsr())
    rhs_lb.append(fleet_ambulances)
    rhs_ub.append(fleet_ambulances)

    # 2. ALS ambulances: sum(a) == round(als_share * fleet_ambulances)
    n_als = int(round(als_share * fleet_ambulances))
    row = sp.dok_matrix((1, n_vars), dtype=np.float64)
    for j in range(n_sites):
        row[0, idx_a + j] = 1.0
    A_rows.append(row.tocsr())
    rhs_lb.append(n_als)
    rhs_ub.append(n_als)

    # 3. Total bikes: sum(y) == fleet_bikes
    if fleet_bikes > 0:
        row = sp.dok_matrix((1, n_vars), dtype=np.float64)
        for j in range(n_sites):
            row[0, idx_y + j] = 1.0
        A_rows.append(row.tocsr())
        rhs_lb.append(fleet_bikes)
        rhs_ub.append(fleet_bikes)

    # 4. Site ambulance cap: a_j + b_j <= MAX_AMBULANCES_PER_SITE
    site_cap_rows = sp.dok_matrix((n_sites, n_vars), dtype=np.float64)
    for j in range(n_sites):
        site_cap_rows[j, idx_a + j] = 1.0
        site_cap_rows[j, idx_b + j] = 1.0
    A_rows.append(site_cap_rows.tocsr())
    rhs_lb.extend([-np.inf] * n_sites)
    rhs_ub.extend([MAX_AMBULANCES_PER_SITE] * n_sites)

    # 5. Coverage linking constraints:
    # z1_i + z2_i + z3_i - sum_{j in N_i}(a_j + b_j) <= 0
    cov3_rows = sp.dok_matrix((n_cells, n_vars), dtype=np.float64)
    for i in range(n_cells):
        cov3_rows[i, idx_z1 + i] = 1.0
        cov3_rows[i, idx_z2 + i] = 1.0
        cov3_rows[i, idx_z3 + i] = 1.0
    # Subtract coverage from A_cov_std
    for i in range(n_cells):
        sites_in = A_cov_std.indices[A_cov_std.indptr[i] : A_cov_std.indptr[i + 1]]
        for s in sites_in:
            cov3_rows[i, idx_a + s] = -1.0
            cov3_rows[i, idx_b + s] = -1.0
    A_rows.append(cov3_rows.tocsr())
    rhs_lb.extend([-np.inf] * n_cells)
    rhs_ub.extend([0.0] * n_cells)

    # za_i - sum_{j in N_i} a_j <= 0 (ALS cover)
    za_rows = sp.dok_matrix((n_cells, n_vars), dtype=np.float64)
    for i in range(n_cells):
        za_rows[i, idx_za + i] = 1.0
        sites_in = A_cov_std.indices[A_cov_std.indptr[i] : A_cov_std.indptr[i + 1]]
        for s in sites_in:
            za_rows[i, idx_a + s] = -1.0
    A_rows.append(za_rows.tocsr())
    rhs_lb.extend([-np.inf] * n_cells)
    rhs_ub.extend([0.0] * n_cells)

    # Bike cover: u_i - sum_{j in N_i^bike} y_j <= 0
    if fleet_bikes > 0 and len(row_bike) > 0:
        u_rows = sp.dok_matrix((n_cells, n_vars), dtype=np.float64)
        for i in range(n_cells):
            u_rows[i, idx_u + i] = 1.0
            sites_in = A_cov_bike.indices[A_cov_bike.indptr[i] : A_cov_bike.indptr[i + 1]]
            for s in sites_in:
                u_rows[i, idx_y + s] = -1.0
        A_rows.append(u_rows.tocsr())
        rhs_lb.extend([-np.inf] * n_cells)
        rhs_ub.extend([0.0] * n_cells)

    # Graded 1.5x cover: z15_i - sum_{j in N_i^1.5}(a_j + b_j) <= 0
    z15_rows = sp.dok_matrix((n_cells, n_vars), dtype=np.float64)
    for i in range(n_cells):
        z15_rows[i, idx_z15 + i] = 1.0
        sites_in = A_cov_15.indices[A_cov_15.indptr[i] : A_cov_15.indptr[i + 1]]
        for s in sites_in:
            z15_rows[i, idx_a + s] = -1.0
            z15_rows[i, idx_b + s] = -1.0
    A_rows.append(z15_rows.tocsr())
    rhs_lb.extend([-np.inf] * n_cells)
    rhs_ub.extend([0.0] * n_cells)

    # Graded 2.0x cover: z20_i - sum_{j in N_i^2.0}(a_j + b_j) <= 0
    z20_rows = sp.dok_matrix((n_cells, n_vars), dtype=np.float64)
    for i in range(n_cells):
        z20_rows[i, idx_z20 + i] = 1.0
        sites_in = A_cov_20.indices[A_cov_20.indptr[i] : A_cov_20.indptr[i + 1]]
        for s in sites_in:
            z20_rows[i, idx_a + s] = -1.0
            z20_rows[i, idx_b + s] = -1.0
    A_rows.append(z20_rows.tocsr())
    rhs_lb.extend([-np.inf] * n_cells)
    rhs_ub.extend([0.0] * n_cells)

    # 6. Ward soft equity floor:
    # (1 / |I_w|) * sum_{i in I_w} z1_i + s_w >= WARD_MIN_COVER_FLOOR (0.50)
    ward_rows = sp.dok_matrix((n_wards, n_vars), dtype=np.float64)
    for i in range(n_cells):  # demand-weighted share of the ward covered in time
        widx = zone_idx[i]
        if ward_lam[widx] > 0:
            ward_rows[widx, idx_z1 + i] += lam[i] / ward_lam[widx]
    for widx in range(n_wards):
        ward_rows[widx, idx_sw + widx] = 1.0
    A_rows.append(ward_rows.tocsr())
    rhs_lb.extend([WARD_MIN_COVER_FLOOR if ward_lam[w] > 0 else 0.0 for w in range(n_wards)])
    rhs_ub.extend([np.inf] * n_wards)

    # 7. Move constraints against prev plan:
    # r_j >= (a_j + b_j) - prev_tot_j  =>  r_j - a_j - b_j >= -prev_tot_j
    # sum(r_j) <= move_cap
    if has_prev:
        move_prev_rows = sp.dok_matrix((n_sites + 1, n_vars), dtype=np.float64)
        for j in range(n_sites):
            move_prev_rows[j, idx_r_prev + j] = 1.0
            move_prev_rows[j, idx_a + j] = -1.0
            move_prev_rows[j, idx_b + j] = -1.0
            # also sum row
            move_prev_rows[n_sites, idx_r_prev + j] = 1.0
        A_rows.append(move_prev_rows.tocsr())
        rhs_lb.extend([-float(prev_tot_plan[j]) for j in range(n_sites)] + [-np.inf])
        rhs_ub.extend([np.inf] * n_sites + [float(move_cap)])

    # 8. Move constraints against next plan:
    if has_next:
        move_next_rows = sp.dok_matrix((n_sites + 1, n_vars), dtype=np.float64)
        for j in range(n_sites):
            move_next_rows[j, idx_r_next + j] = 1.0
            move_next_rows[j, idx_a + j] = -1.0
            move_next_rows[j, idx_b + j] = -1.0
            move_next_rows[n_sites, idx_r_next + j] = 1.0
        A_rows.append(move_next_rows.tocsr())
        rhs_lb.extend([-float(next_tot_plan[j]) for j in range(n_sites)] + [-np.inf])
        rhs_ub.extend([np.inf] * n_sites + [float(move_cap)])

    # Stack constraints
    A_constr = sp.vstack(A_rows, format="csr")
    bounds = Bounds(lb=lb, ub=ub)
    linear_constraints = LinearConstraint(A_constr, lb=rhs_lb, ub=rhs_ub)

    # Solve MILP with HiGHS
    res = milp(
        c=c,
        integrality=integrality,
        bounds=bounds,
        constraints=linear_constraints,
        options={
            "time_limit": time_limit_sec,
            "mip_rel_gap": 0.005,
            "disp": False,
        },
    )

    if not res.success and (res.x is None or len(res.x) == 0):
        print(f"[optimize] Warning: HiGHS solve status {res.status} ({res.message}). Retrying with 2x time limit...")
        res = milp(
            c=c,
            integrality=integrality,
            bounds=bounds,
            constraints=linear_constraints,
            options={
                "time_limit": time_limit_sec * 2.0,
                "mip_rel_gap": 0.02,
                "disp": False,
            },
        )

    # Never invent a plan: a failed solve raises, and the caller falls back to something real
    if res.x is None or len(res.x) != n_vars:
        raise RuntimeError(f"HiGHS found no solution for {reg_code} block {block_id}: {res.message}")
    sol = res.x
    a_sol = np.round(sol[idx_a : idx_a + n_sites]).astype(np.int32)
    b_sol = np.round(sol[idx_b : idx_b + n_sites]).astype(np.int32)
    y_sol = np.round(sol[idx_y : idx_y + n_sites]).astype(np.int32)

    # Ensure integer bounds exact
    tot_amb = int(a_sol.sum() + b_sol.sum())
    if tot_amb != fleet_ambulances:
        diff = fleet_ambulances - tot_amb
        # adjust BLS at top site
        b_sol[np.argmax(b_sol)] = max(0, b_sol[np.argmax(b_sol)] + diff)

    # Compute analytic expected cover per cell
    exp_cov, nearest_m, bike_cov = compute_expected_cover(
        a_sol, b_sol, y_sol, drive_min, bike_drive_min, q, target_min
    )

    return BlockPlan(
        block_id=block_id,
        a=a_sol,
        b=b_sol,
        y=y_sol,
        q=q,
        busy_fraction=q,
        expected_cover=exp_cov,
        nearest_min=nearest_m,
        bike_cover=bike_cov,
        objective_value=float(res.fun if res.fun is not None else 0.0),
    )


def compute_expected_cover(
    a: np.ndarray,
    b: np.ndarray,
    y: np.ndarray,
    drive_min: np.ndarray,
    bike_drive_min: np.ndarray,
    q: float,
    target_min: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calculate cell-level analytic coverage probability and nearest standby drive time."""
    n_sites, n_cells = drive_min.shape
    tot_amb = a + b

    t_target = target_min - PRE_TRAVEL_MIN
    t_15 = 1.5 * target_min - PRE_TRAVEL_MIN
    t_20 = 2.0 * target_min - PRE_TRAVEL_MIN
    t_bike = 10.0 - PRE_TRAVEL_MIN

    # Nearest minutes: only considering sites with at least 1 ambulance deployed
    deployed_sites = np.where(tot_amb > 0)[0]
    if len(deployed_sites) > 0:
        nearest_min = drive_min[deployed_sites, :].min(axis=0) + PRE_TRAVEL_MIN
    else:
        nearest_min = drive_min.min(axis=0) + PRE_TRAVEL_MIN

    # Ambulances covering each cell within target
    cov_amb_counts = np.zeros(n_cells, dtype=np.int32)
    cov_15_counts = np.zeros(n_cells, dtype=np.int32)
    cov_20_counts = np.zeros(n_cells, dtype=np.int32)

    for j in range(n_sites):
        if tot_amb[j] > 0:
            cov_amb_counts += np.where(drive_min[j, :] <= t_target, tot_amb[j], 0)
            cov_15_counts += np.where(drive_min[j, :] <= t_15, tot_amb[j], 0)
            cov_20_counts += np.where(drive_min[j, :] <= t_20, tot_amb[j], 0)

    # Base MEXCLP expected cover: 1 - q^k
    # Graded partial credit: + 0.4 for [t_target, 1.5], + 0.2 for [1.5, 2.0]
    base_cov = 1.0 - (q ** cov_amb_counts)
    cov_15 = 1.0 - (q ** cov_15_counts)
    cov_20 = 1.0 - (q ** cov_20_counts)

    graded_cov = base_cov + 0.40 * np.maximum(0.0, cov_15 - base_cov) + 0.20 * np.maximum(0.0, cov_20 - cov_15)
    graded_cov = np.clip(graded_cov, 0.0, 1.0)

    # Bike coverage
    bike_sites = np.where(y > 0)[0]
    bike_cov = np.zeros(n_cells, dtype=np.float32)
    if len(bike_sites) > 0:
        bike_counts = np.zeros(n_cells, dtype=np.int32)
        for j in bike_sites:
            bike_counts += np.where(bike_drive_min[j, :] <= t_bike, y[j], 0)
        bike_cov = (1.0 - (q ** bike_counts)).astype(np.float32)

    return graded_cov.astype(np.float32), nearest_min.astype(np.float32), bike_cov


def compute_baseline(reg_code: str, availability: float = BASE_AVAILABILITY) -> List[BlockPlan]:
    """Baseline (Today per CAG): ambulances at hospitals proportional to catchment population."""
    cfg = REGIONS[reg_code]
    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")
    travel = np.load(PROCESSED_DIR / f"travel_{reg_code}.npz")
    fwd = travel["fwd"]

    n_sites = len(df_sites)
    n_cells = len(df_cells)
    fleet = on_road(cfg.fleet_ambulances, availability)
    n_als = int(round(cfg.als_share * fleet))

    # Hospital sites
    hosp_indices = np.where(df_sites["site_type"] == "hospital")[0]
    if len(hosp_indices) == 0:
        hosp_indices = np.arange(n_sites)

    # Assign each cell to nearest hospital
    hosp_fwd = fwd[hosp_indices, :]
    nearest_hosp_local = np.argmin(hosp_fwd, axis=0)
    nearest_hosp = hosp_indices[nearest_hosp_local]

    # Catchment populations
    cell_pops = df_cells["pop"].values
    catchment_pop = np.zeros(n_sites, dtype=np.float64)
    for c_idx in range(n_cells):
        catchment_pop[nearest_hosp[c_idx]] += cell_pops[c_idx]

    # Hamilton-Hare largest-remainder quota distribution
    tot_pop = catchment_pop.sum()
    exact_quotas = (catchment_pop / tot_pop) * fleet
    int_quotas = np.floor(exact_quotas).astype(np.int32)
    remainders = exact_quotas - int_quotas

    # Cap at MAX_AMBULANCES_PER_SITE
    int_quotas = np.clip(int_quotas, 0, MAX_AMBULANCES_PER_SITE)
    rem_needed = fleet - int_quotas.sum()
    if rem_needed > 0:
        sort_rem = np.argsort(-remainders)
        for s in sort_rem:
            if int_quotas[s] < MAX_AMBULANCES_PER_SITE:
                int_quotas[s] += 1
                rem_needed -= 1
                if rem_needed == 0:
                    break

    # Assign ALS to largest catchments
    a_base = np.zeros(n_sites, dtype=np.int32)
    b_base = int_quotas.copy()
    sort_catch = np.argsort(-catchment_pop)
    als_assigned = 0
    for s in sort_catch:
        if b_base[s] > 0 and als_assigned < n_als:
            a_base[s] += 1
            b_base[s] -= 1
            als_assigned += 1

    y_base = np.zeros(n_sites, dtype=np.int32)  # Today has no bikes active

    # Build plans across 6 blocks (identical allocation all day)
    plans = []
    hourly_factors = (
        list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24
    )
    df_lam = pd.read_parquet(PROCESSED_DIR / f"lam_{reg_code}.parquet")

    for b_id in range(6):
        hours = PLANNING_BLOCKS[b_id]["hours"]
        factor = float(np.mean([hourly_factors[h] for h in hours]))
        drive_min = fwd * factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
        bike_drive_min = fwd * (1.0 + 0.4 * (factor - 1.0)) / 60.0
        lam = df_lam[f"block_{b_id}"].values
        q = compute_busy_fraction(lam, drive_min, fleet)

        exp_cov, nearest_m, bike_cov = compute_expected_cover(
            a_base, b_base, y_base, drive_min, bike_drive_min, q, cfg.target_min
        )
        plans.append(
            BlockPlan(
                block_id=b_id,
                a=a_base.copy(),
                b=b_base.copy(),
                y=y_base.copy(),
                q=q,
                busy_fraction=q,
                expected_cover=exp_cov,
                nearest_min=nearest_m,
                bike_cover=bike_cov,
            )
        )

    print(f"[optimize] Baseline for {reg_code}: {fleet} ambulances deployed across {np.count_nonzero(int_quotas)} hospitals.")
    return plans


def solve_static_plan(reg_code: str, availability: float = BASE_AVAILABILITY) -> List[BlockPlan]:
    """Static Plan: One fixed deployment all day, optimized for all-day mean demand and peak traffic, no bikes."""
    cfg = REGIONS[reg_code]
    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")
    df_lam = pd.read_parquet(PROCESSED_DIR / f"lam_{reg_code}.parquet")
    travel = np.load(PROCESSED_DIR / f"travel_{reg_code}.npz")
    fwd = travel["fwd"]

    # Evening peak traffic
    hourly_factors = (
        list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24
    )
    peak_factor = float(np.mean([hourly_factors[h] for h in PLANNING_BLOCKS[4]["hours"]]))
    peak_drive_min = fwd * peak_factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
    bike_drive_min = peak_drive_min.copy()

    mean_lam = df_lam["all_day_mean"].values
    n_amb = on_road(cfg.fleet_ambulances, availability)

    print(f"[optimize] Solving Static Plan for {reg_code} (All-day mean demand, peak traffic, no bikes)...")
    t0 = time.time()
    plan_peak = solve_single_mexclp(
        reg_code=reg_code,
        block_id=4,
        lam=mean_lam,
        drive_min=peak_drive_min,
        bike_drive_min=bike_drive_min,
        df_cells=df_cells,
        df_sites=df_sites,
        fleet_ambulances=n_amb,
        als_share=cfg.als_share,
        fleet_bikes=0,  # Static plan does not use bikes
        time_limit_sec=40.0,
    )
    print(f"[optimize] Static plan solved in {time.time() - t0:.1f}s.")

    # Replicate for all 6 blocks with respective block demand & traffic
    static_plans = []
    for b_id in range(6):
        hours = PLANNING_BLOCKS[b_id]["hours"]
        factor = float(np.mean([hourly_factors[h] for h in hours]))
        drive_min = fwd * factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
        bike_drive_min = fwd * (1.0 + 0.4 * (factor - 1.0)) / 60.0
        lam = df_lam[f"block_{b_id}"].values
        q = compute_busy_fraction(lam, drive_min, n_amb)

        exp_cov, nearest_m, bike_cov = compute_expected_cover(
            plan_peak.a, plan_peak.b, plan_peak.y, drive_min, bike_drive_min, q, cfg.target_min
        )
        static_plans.append(
            BlockPlan(
                block_id=b_id,
                a=plan_peak.a.copy(),
                b=plan_peak.b.copy(),
                y=plan_peak.y.copy(),
                q=q,
                busy_fraction=q,
                expected_cover=exp_cov,
                nearest_min=nearest_m,
                bike_cover=bike_cov,
            )
        )
    return static_plans


def solve_goldenhour_rolling(
    reg_code: str,
    fleet_ambulances: Optional[int] = None,
    fleet_bikes: Optional[int] = None,
    demand_scale: float = 1.0,
    traffic_scale: float = 1.0,
    availability: float = BASE_AVAILABILITY,
    lam_multiplier: Optional[np.ndarray] = None,
    fwd: Optional[np.ndarray] = None,
    df_sites: Optional[pd.DataFrame] = None,
    time_limit_scale: float = 1.0,
) -> List[BlockPlan]:
    """Solve GoldenHour multi-period rolling horizon MILP with move smoothing.

    lam_multiplier: per-cell priority weights (e.g. from black spots). Each block's demand is reweighted and
        rescaled to its original total, so priorities shift but the busy-fraction estimate is unchanged.
    fwd, df_sites: an extended candidate set (e.g. with proposed standby posts); default is the region's own.
    time_limit_scale: scales every HiGHS time limit (below 1 for quick runs).
    """
    cfg = REGIONS[reg_code]
    # fleet_* are the physical fleet; only the available share is planned
    n_amb = on_road(fleet_ambulances if fleet_ambulances is not None else cfg.fleet_ambulances, availability)
    n_bikes = on_road(fleet_bikes if fleet_bikes is not None else cfg.fleet_bikes, availability)
    move_cap = int(np.floor(MOVES_CAP_FRACTION * n_amb))

    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    if df_sites is None:
        df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")
    df_lam = pd.read_parquet(PROCESSED_DIR / f"lam_{reg_code}.parquet")
    if fwd is None:
        fwd = np.load(PROCESSED_DIR / f"travel_{reg_code}.npz")["fwd"]
    if fwd.shape[0] != len(df_sites):
        raise ValueError(f"fwd has {fwd.shape[0]} site rows but df_sites has {len(df_sites)}")

    hourly_factors = (
        list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24
    )
    # Apply traffic scale
    hourly_factors = [f * traffic_scale for f in hourly_factors]

    # Precalculate drive matrices for each block
    block_drive = {}
    block_bike_drive = {}
    block_lam = {}

    for b_id in range(6):
        hours = PLANNING_BLOCKS[b_id]["hours"]
        factor = float(np.mean([hourly_factors[h] for h in hours]))
        block_drive[b_id] = fwd * factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
        block_bike_drive[b_id] = fwd * (1.0 + 0.4 * (factor - 1.0)) / 60.0
        lam = df_lam[f"block_{b_id}"].values * demand_scale
        if lam_multiplier is not None:
            weighted = lam * lam_multiplier
            lam = weighted * (lam.sum() / weighted.sum())
        block_lam[b_id] = lam

    print(
        f"\n[optimize] Starting Rolling Horizon MEXCLP for {reg_code} "
        f"(fleet: {n_amb} amb, {n_bikes} bikes, move_cap: {move_cap})..."
    )
    t_start = time.time()
    plans: Dict[int, BlockPlan] = {}

    def solve_block(b_id: int, prev=None, nxt=None, time_limit_sec: float = 30.0) -> BlockPlan:
        kw = dict(reg_code=reg_code, block_id=b_id, lam=block_lam[b_id], drive_min=block_drive[b_id],
                  bike_drive_min=block_bike_drive[b_id], df_cells=df_cells, df_sites=df_sites,
                  fleet_ambulances=n_amb, als_share=cfg.als_share, fleet_bikes=n_bikes,
                  time_limit_sec=time_limit_sec * time_limit_scale)
        try:
            return solve_single_mexclp(**kw, prev_tot_plan=prev, next_tot_plan=nxt,
                                       move_cap=move_cap if (prev is not None or nxt is not None) else None)
        except RuntimeError as e:  # move caps can make a block too hard; drop them rather than fail
            print(f"[optimize] {e}. Re-solving block {b_id} without move caps.")
            return solve_single_mexclp(**kw)

    # Step 1: Solve Block 4 (Evening Peak) unconstrained
    print("[optimize] Solving Block 4 (Evening Peak, 16:00-20:00) unconstrained...")
    plans[4] = solve_block(4)

    # Step 2: Forward roll: Block 5 -> 0 -> 1 -> 2 -> 3, each capped against the block before
    for curr_b, prev_b in [(5, 4), (0, 5), (1, 0), (2, 1), (3, 2)]:
        print(f"[optimize] Solving Block {curr_b} move-capped against Block {prev_b}...")
        plans[curr_b] = solve_block(curr_b, prev=plans[prev_b].a + plans[prev_b].b, time_limit_sec=25.0)

    # Step 3: Re-solve Block 4 capped against both Block 3 and Block 5 (closes the daily loop)
    print("[optimize] Re-solving Block 4 constrained against Block 3 and Block 5 (closing cyclic shift loop)...")
    plans[4] = solve_block(4, prev=plans[3].a + plans[3].b, nxt=plans[5].a + plans[5].b)

    ordered_plans = [plans[i] for i in range(6)]
    print(f"[optimize] Rolling horizon for {reg_code} finished in {time.time() - t_start:.1f}s.")
    return ordered_plans


def compute_crew_moves(
    plans: List[BlockPlan],
    ss_matrix: np.ndarray,
    df_sites: pd.DataFrame,
) -> List[Dict[str, Any]]:
    """Compute exact vehicle-to-vehicle standby moves between blocks using SciPy Linear Sum Assignment."""
    all_block_moves = []
    n_sites = len(df_sites)

    for b in range(6):
        next_b = (b + 1) % 6
        p1 = plans[b]
        p2 = plans[next_b]

        tot1 = p1.a + p1.b
        tot2 = p2.a + p2.b

        diff = tot2 - tot1
        # Sites where vehicles depart (diff < 0) and arrive (diff > 0)
        dep_sites = []
        for s in np.where(diff < 0)[0]:
            dep_sites.extend([s] * (-diff[s]))

        arr_sites = []
        for s in np.where(diff > 0)[0]:
            arr_sites.extend([s] * diff[s])

        n_moves = len(dep_sites)
        moves_list = []
        if n_moves > 0 and len(arr_sites) == n_moves:
            # Cost matrix: travel time in minutes between dep and arr sites
            cost_matrix = np.zeros((n_moves, n_moves), dtype=np.float32)
            for i, s_from in enumerate(dep_sites):
                for j, s_to in enumerate(arr_sites):
                    cost_matrix[i, j] = ss_matrix[s_from, s_to] / 60.0

            row_ind, col_ind = linear_sum_assignment(cost_matrix)
            for r, c in zip(row_ind, col_ind):
                s_from = dep_sites[r]
                s_to = arr_sites[c]
                t_min = float(cost_matrix[r, c])
                moves_list.append({
                    "from_site_id": df_sites.iloc[s_from]["site_id"],
                    "from_name": df_sites.iloc[s_from]["name"],
                    "from_lat": float(df_sites.iloc[s_from]["lat"]),
                    "from_lon": float(df_sites.iloc[s_from]["lon"]),
                    "to_site_id": df_sites.iloc[s_to]["site_id"],
                    "to_name": df_sites.iloc[s_to]["name"],
                    "to_lat": float(df_sites.iloc[s_to]["lat"]),
                    "to_lon": float(df_sites.iloc[s_to]["lon"]),
                    "travel_min": round(t_min, 1),
                })

        all_block_moves.append({
            "from_block": b,
            "to_block": next_b,
            "count": len(moves_list),
            "moves": moves_list,
        })

    return all_block_moves
