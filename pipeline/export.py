"""GoldenHour: Static Data Export Pipeline for Web Application.

Exports precomputed models, plans, simulations, stress tests, and geographic data
into static JSON payloads in web/public/data/:
  - cells_{region}.json
  - sites_{region}.json
  - scenarios_{region}.json
  - meta.json
  - zones.geojson
"""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import numpy as np
import pandas as pd

from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    BENGALURU_HOURLY_CONGESTION,
    CALL_MIX,
    PLANNING_BLOCKS,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    REGIONS,
    WEB_DATA_DIR,
)
from pipeline.optimize import (
    BlockPlan,
    compute_baseline,
    compute_crew_moves,
    compute_expected_cover,
    solve_goldenhour_rolling,
    solve_single_mexclp,
    solve_static_plan,
)
from pipeline.simulate import SimulationResults, simulate_plan


def format_sparse_deployments(plan: BlockPlan, df_sites: pd.DataFrame) -> List[Dict[str, Any]]:
    """Format non-zero deployments for JSON export."""
    tot = plan.a + plan.b + plan.y
    active = np.where(tot > 0)[0]
    out = []
    for s in active:
        out.append({
            "site_id": str(df_sites.iloc[s]["site_id"]),
            "name": str(df_sites.iloc[s]["name"]),
            "site_type": str(df_sites.iloc[s]["site_type"]),
            "lat": round(float(df_sites.iloc[s]["lat"]), 5),
            "lon": round(float(df_sites.iloc[s]["lon"]), 5),
            "a": int(plan.a[s]),
            "b": int(plan.b[s]),
            "y": int(plan.y[s]),
        })
    return out


def compute_reachability(reg_code: str, fwd: np.ndarray, target_min: float) -> List[bool]:
    """Check if each cell can be reached within target from ANY site at peak traffic."""
    cfg = REGIONS[reg_code]
    hourly_factors = (
        list(BENGALURU_HOURLY_CONGESTION) if cfg.congestion_enabled else [1.10] * 24
    )
    peak_factor = float(np.mean([hourly_factors[h] for h in PLANNING_BLOCKS[4]["hours"]]))
    peak_drive_min = fwd.min(axis=0) * peak_factor / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
    total_peak_time = peak_drive_min + PRE_TRAVEL_MIN
    return (total_peak_time <= target_min).tolist()


def run_stress_tests(
    reg_code: str,
    base_plans: List[BlockPlan],
    gh_plans: List[BlockPlan],
    df_calls: pd.DataFrame,
    df_cells: pd.DataFrame,
    df_sites: pd.DataFrame,
    fwd: np.ndarray,
    back: np.ndarray,
    ss: np.ndarray,
) -> List[Dict[str, Any]]:
    """Run stress test battery: Normal, Demand +20%, Traffic +20%, Fleet 60%."""
    cfg = REGIONS[reg_code]
    print(f"\n[export] Running stress tests for {reg_code}...")

    # 1. Normal
    sim_base_norm = simulate_plan(reg_code, base_plans, df_calls, df_cells, df_sites, fwd, back, ss)
    sim_gh_norm = simulate_plan(reg_code, gh_plans, df_calls, df_cells, df_sites, fwd, back, ss)

    # 2. Demand +20% (sample extra 20% calls)
    print("[export] Stress test: Demand +20%...")
    calls_20 = pd.concat([df_calls, df_calls.sample(frac=0.20, random_state=42)]).sort_values("timestamp")
    sim_base_dem = simulate_plan(reg_code, base_plans, calls_20, df_cells, df_sites, fwd, back, ss)
    sim_gh_dem = simulate_plan(reg_code, gh_plans, calls_20, df_cells, df_sites, fwd, back, ss)

    # 3. Traffic +20%
    print("[export] Stress test: Traffic +20%...")
    fwd_traffic = fwd * 1.20
    back_traffic = back * 1.20
    sim_base_traf = simulate_plan(reg_code, base_plans, df_calls, df_cells, df_sites, fwd_traffic, back_traffic, ss)
    sim_gh_traf = simulate_plan(reg_code, gh_plans, df_calls, df_cells, df_sites, fwd_traffic, back_traffic, ss)

    # 4. Only 60% of the fleet on the road (instead of ~74%): both plans are re-planned with fewer vehicles
    print("[export] Stress test: only 60% of the fleet on the road (re-planned)...")
    gh_plans_60 = solve_goldenhour_rolling(reg_code, availability=0.60)
    base_plans_60 = compute_baseline(reg_code, availability=0.60)

    sim_base_60 = simulate_plan(reg_code, base_plans_60, df_calls, df_cells, df_sites, fwd, back, ss)
    sim_gh_60 = simulate_plan(reg_code, gh_plans_60, df_calls, df_cells, df_sites, fwd, back, ss)

    return [
        {
            "name": "Standard Operations",
            "description": "About 74% of the fleet on the road, normal traffic and demand",
            "baseline_within_pct": sim_base_norm.within_target_pct,
            "goldenhour_within_pct": sim_gh_norm.within_target_pct,
            "baseline_median": sim_base_norm.median_min,
            "goldenhour_median": sim_gh_norm.median_min,
            "baseline_p90": sim_base_norm.p90_min,
            "goldenhour_p90": sim_gh_norm.p90_min,
            "first_help_gain_pct": round(sim_gh_norm.first_help_10m_pct - sim_base_norm.first_help_10m_pct, 1),
        },
        {
            "name": "Demand Surge (+20%)",
            "description": "20% higher emergency call volume across all wards",
            "baseline_within_pct": sim_base_dem.within_target_pct,
            "goldenhour_within_pct": sim_gh_dem.within_target_pct,
            "baseline_median": sim_base_dem.median_min,
            "goldenhour_median": sim_gh_dem.median_min,
            "baseline_p90": sim_base_dem.p90_min,
            "goldenhour_p90": sim_gh_dem.p90_min,
            "first_help_gain_pct": round(sim_gh_dem.first_help_10m_pct - sim_base_dem.first_help_10m_pct, 1),
        },
        {
            "name": "Severe Traffic Congestion (+20%)",
            "description": "20% increase in road travel times during peak hours",
            "baseline_within_pct": sim_base_traf.within_target_pct,
            "goldenhour_within_pct": sim_gh_traf.within_target_pct,
            "baseline_median": sim_base_traf.median_min,
            "goldenhour_median": sim_gh_traf.median_min,
            "baseline_p90": sim_base_traf.p90_min,
            "goldenhour_p90": sim_gh_traf.p90_min,
            "first_help_gain_pct": round(sim_gh_traf.first_help_10m_pct - sim_base_traf.first_help_10m_pct, 1),
        },
        {
            "name": "Fleet Breakdown (60% Available)",
            "description": "Only 60% of the fleet on the road (instead of ~74%); both plans re-planned",
            "baseline_within_pct": sim_base_60.within_target_pct,
            "goldenhour_within_pct": sim_gh_60.within_target_pct,
            "baseline_median": sim_base_60.median_min,
            "goldenhour_median": sim_gh_60.median_min,
            "baseline_p90": sim_base_60.p90_min,
            "goldenhour_p90": sim_gh_60.p90_min,
            "first_help_gain_pct": round(sim_gh_60.first_help_10m_pct - sim_base_60.first_help_10m_pct, 1),
        },
    ]


def compute_fleet_size_curve(
    reg_code: str,
    df_calls: pd.DataFrame,
    df_cells: pd.DataFrame,
    df_sites: pd.DataFrame,
    fwd: np.ndarray,
    back: np.ndarray,
    ss: np.ndarray,
) -> List[Dict[str, Any]]:
    """How many vehicles does the region need? Re-plan with GoldenHour at 0.75x-2x today's fleet and
    replay the same 4 weeks of calls. Uses the same simulation as every other number on the dashboard."""
    cfg = REGIONS[reg_code]
    print(f"[export] Computing fleet-size curve for {reg_code}...")
    curve = []
    for m in [0.75, 1.0, 1.25, 1.5, 2.0]:
        fleet_size = int(round(cfg.fleet_ambulances * m))
        bike_size = int(round(cfg.fleet_bikes * m))
        plans = solve_goldenhour_rolling(reg_code, fleet_ambulances=fleet_size, fleet_bikes=bike_size)
        sim = simulate_plan(reg_code, plans, df_calls, df_cells, df_sites, fwd, back, ss)
        curve.append({
            "multiplier": m,
            "ambulances": fleet_size,          # physical fleet
            "on_road": int(plans[0].a.sum() + plans[0].b.sum()),
            "bikes": bike_size,
            "within_target_pct": sim.within_target_pct,
            "median_min": sim.median_min,
            "p90_min": sim.p90_min,
        })
    return curve


def write_cells_json(reg_code: str, out_path: Path) -> None:
    """cells_{r}.json: one record per hexagon with its forecast calls per hour for each block."""
    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    df_lam = pd.read_parquet(PROCESSED_DIR / f"lam_{reg_code}.parquet").set_index("h3_index")
    lam = df_lam.loc[df_cells["h3_index"], [f"block_{b}" for b in range(6)]].to_numpy()
    out = [
        {
            "h3": r.h3_index,
            "lat": round(float(r.lat), 5),
            "lon": round(float(r.lon), 5),
            "zone": str(r.zone),
            "pop": int(round(float(r.pop))),
            "major_km": round(float(r.major_km), 2),
            "lam": [round(float(v), 4) for v in lam[i]],
        }
        for i, r in enumerate(df_cells.itertuples(index=False))
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"[export] Written {len(out)} cells to {out_path.name}")


def write_sites_json(reg_code: str, out_path: Path) -> None:
    """sites_{r}.json: every candidate standby site."""
    df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")
    out = [
        {
            "site_id": str(r.site_id),
            "name": str(r.name),
            "site_type": str(r.site_type),
            "lat": round(float(r.lat), 5),
            "lon": round(float(r.lon), 5),
            "h3": str(r.h3_index),
        }
        for r in df_sites.itertuples(index=False)
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"[export] Written {len(out)} sites to {out_path.name}")


def export_region(reg_code: str):
    """Run optimization, simulation, and export JSON artifacts for one region."""
    cfg = REGIONS[reg_code]
    print(f"\n{'=' * 70}\nExporting Region: {reg_code.upper()} ({cfg.name})\n{'=' * 70}")

    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")
    df_calls = pd.read_parquet(PROCESSED_DIR / f"calls_{reg_code}.parquet")
    travel = np.load(PROCESSED_DIR / f"travel_{reg_code}.npz")
    fwd = travel["fwd"]
    back = travel["back"]
    ss = travel["ss"]

    # 1-2. Export cells_{r}.json and sites_{r}.json
    write_cells_json(reg_code, WEB_DATA_DIR / f"cells_{reg_code}.json")
    write_sites_json(reg_code, WEB_DATA_DIR / f"sites_{reg_code}.json")

    # 3. Reachability
    reach = compute_reachability(reg_code, fwd, cfg.target_min)

    # 4. Compute Scenarios: Baseline, Static, GoldenHour
    print(f"\n[export] Computing Baseline scenario for {reg_code}...")
    base_plans = compute_baseline(reg_code)
    sim_base = simulate_plan(reg_code, base_plans, df_calls, df_cells, df_sites, fwd, back, ss)

    print(f"\n[export] Computing Static Plan scenario for {reg_code}...")
    static_plans = solve_static_plan(reg_code)
    sim_static = simulate_plan(reg_code, static_plans, df_calls, df_cells, df_sites, fwd, back, ss)

    print(f"\n[export] Computing GoldenHour Rolling Horizon scenario for {reg_code}...")
    gh_plans = solve_goldenhour_rolling(reg_code)
    sim_gh = simulate_plan(reg_code, gh_plans, df_calls, df_cells, df_sites, fwd, back, ss)

    # Crew moves for GoldenHour
    gh_moves = compute_crew_moves(gh_plans, ss, df_sites)

    # Format plans for JSON
    def package_scenario(plans: List[BlockPlan], sim: SimulationResults, moves=None):
        return {
            "fleet": {
                "ambulances": int(plans[0].a.sum() + plans[0].b.sum()),
                "als": int(plans[0].a.sum()),
                "bls": int(plans[0].b.sum()),
                "bikes": int(plans[0].y.sum()),
            },
            "plans": [
                {
                    "block_id": p.block_id,
                    "q": round(p.q, 3),
                    "cover": [round(float(v), 3) for v in p.expected_cover],
                    "nearest_min": [round(float(v), 1) for v in p.nearest_min],
                    "bike_cover": [round(float(v), 3) for v in p.bike_cover],
                    "deployments": format_sparse_deployments(p, df_sites),
                }
                for p in plans
            ],
            "sim": asdict(sim),
            "moves": moves if moves is not None else [],
        }

    scenario_payload = {
        "region": reg_code,
        "region_name": cfg.name,
        "target_min": cfg.target_min,
        "reach": reach,
        "baseline": package_scenario(base_plans, sim_base),
        "static": package_scenario(static_plans, sim_static),
        "goldenhour": package_scenario(gh_plans, sim_gh, gh_moves),
        "stress_tests": run_stress_tests(reg_code, base_plans, gh_plans, df_calls, df_cells, df_sites, fwd, back, ss),
        "fleet_curve": compute_fleet_size_curve(reg_code, df_calls, df_cells, df_sites, fwd, back, ss),
    }

    out_scenarios = WEB_DATA_DIR / f"scenarios_{reg_code}.json"
    with open(out_scenarios, "w", encoding="utf-8") as f:
        json.dump(scenario_payload, f)
    print(f"[export] Saved scenarios to {out_scenarios} ({out_scenarios.stat().st_size / 1e6:.2f} MB)")


def build_metadata() -> Dict[str, Any]:
    """meta.json content: study assumptions, references, and blocks."""
    return {
        "title": "GoldenHour",
        "subtitle": "Karnataka 108 Emergency Ambulance Fleet Allocation & Response Planner",
        "team": "Super Intelligence (DSCE) - PES University Zero Hour Hackathon Track 3",
        "blocks": PLANNING_BLOCKS,
        "call_mix": CALL_MIX,
        "regions": {
            k: {
                "name": v.name,
                "code": v.code,
                "target_min": v.target_min,
                "zone_label": v.zone_label,
                "fleet_ambulances": v.fleet_ambulances,
                "fleet_bikes": v.fleet_bikes,
                "calls_per_day": v.calls_per_day,
                "description": v.description,
            }
            for k, v in REGIONS.items()
        },
        "assumptions": [
            {
                "symbol": "◐",
                "label": "Government Report",
                "source": "CAG Report No. 3 of 2020 on Karnataka 108 GVK-EMRI",
                "details": "72% of urban trips met the 20-min target (2014-19); triage over 3 min in 47% of calls; roll-out over 1 min in 85%; 41.94 lakh trips in 5 years.",
            },
            {
                "symbol": "◐",
                "label": "Fleet Availability",
                "source": "News reports (2025)",
                "details": "About 715 ambulances statewide, about 530 on the road (~74%); about 25% under maintenance.",
            },
            {
                "symbol": "◐",
                "label": "Cabinet Decision",
                "source": "Karnataka State Cabinet (Sep 2026)",
                "details": "Approved privatising field operations with targets of 10 min rural and 15-20 min urban, and fines of Rs 5,000-10,000 (Daijiworld, News Karnataka). No tender document found yet.",
            },
            {
                "symbol": "◐",
                "label": "Command Centre",
                "source": "C-DAC NG-ERSS Command Centre (May 2026)",
                "details": "Centralized 112/108 dispatch system running GPS tracking and nearest-free ambulance dispatch.",
            },
            {
                "symbol": "◐",
                "label": "Traffic Index",
                "source": "TomTom Bengaluru Traffic Index 2025",
                "details": "36 minutes per 10 km average journey time; rush-hour speeds ~14 km/h; peak congestion multiplier 2.6x.",
            },
            {
                "symbol": "≈",
                "label": "Calibrated Model",
                "source": "GoldenHour Optimization & Simulation Framework",
                "details": "Pre-travel time 3.0 min; On-scene Gamma(15m); Hospital handover Gamma(20m); Ambulance siren advantage 1.15x.",
            },
        ],
    }


def export_metadata():
    """Export meta.json with study assumptions, references, and blocks."""
    print("\n[export] Exporting meta.json...")
    meta = build_metadata()
    with open(WEB_DATA_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[export] meta.json written to {WEB_DATA_DIR / 'meta.json'}")


def main():
    print("=" * 70)
    print("GoldenHour: Static JSON Data Export Pipeline")
    print("=" * 70)
    t_start = time.time()

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    export_metadata()
    export_region("blr")
    export_region("tmk")

    print("\n" + "=" * 70)
    print(f"Export pipeline completed in {time.time() - t_start:.1f}s!")
    print(f"Data files written to {WEB_DATA_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
