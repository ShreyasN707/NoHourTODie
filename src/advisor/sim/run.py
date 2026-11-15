"""Run the replay and sensitivity sweeps; write results/advisor_sim/sim_results.json and summary.csv.

    PYTHONPATH=src .venv/bin/python -m advisor.sim.run            # full: 28-day base + sweeps
    PYTHONPATH=src .venv/bin/python -m advisor.sim.run --quick    # 1-day base + 2 sweeps, for dev

Output schema: docs/advisor_contract.md. EVERYTHING in these files is a SIMULATION RESULT on synthetic calls,
fictional hospitals and simulated ED behaviour; the banner says so and the UI must show it.

Determinism: the same config gives byte-identical files apart from `generated_at` (sorted keys, rounded
floats, seeded random streams, scenarios collected in a fixed order whatever the worker count).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from advisor.config import DEFAULT_CONFIG, ROOT_DIR, parse_config
from advisor.graph.osm import DEFAULT_PROCESSED_DIR
from advisor.graph.travel import haversine_km
from advisor.models import Hospital, LatLon
from advisor.provenance import DEMO_BANNER, Provenance
from advisor.registry.demo import build_demo_registry, load_active_registry
from advisor.sim.incidents import GH_DIR, Incident, load_cell_centres, load_goldenhour_calls
from advisor.sim.patients import critical_categories
from advisor.sim.replay import POLICIES, Draws, World, metrics, registry_view, run_policy
from advisor.sim.sweeps import Sweep, default_sweeps, quick_sweeps
from advisor.sim.travel_matrix import Point, SimTravel, TravelTables, load_or_compute_tables

RESULTS_DIR = ROOT_DIR / "results" / "advisor_sim"
QUICK_DIR = DEFAULT_PROCESSED_DIR / "advisor_sim_quick"  # gitignored; --quick never overwrites committed results
SITES_BLR = GH_DIR / "02_geography" / "standby_sites_blr.csv"
QUICK_DAYS = 1

DEFINITIONS = {
    "time_to_treatment": "Minutes from the crew being ready to leave the scene (before any pre-alert wait) until the "
                         "patient gets an ED bay at a hospital that can treat their TRUE (sampled) care category. "
                         "Patients who never reach such a hospital are excluded from the statistic and counted "
                         "in no_definitive_care.",
    "critical": "Care categories with lambda 0 in the base config, excluding arrest_airway: {critical}.",
    "true_category": "Sampled from the call's complaint and config sim.* shares, never from the advisor's triage. "
                     "Over-triaged patients (e.g. no-ECG chest pain that is not STEMI) count as low_acuity.",
    "secondary_transfers": "Patients moved on at least once after arriving at a hospital (refused, or it lacked the "
                           "capability for the true category).",
    "critical_capable_first_attempt_share": "Share of critical patients treated at the first hospital the "
                                            "ambulance reached (it had the capability and took them).",
    "ambulance_hours": "Sum over incidents of hours from scene-ready until the ambulance is back at its base "
                       "(nearest GoldenHour standby site to the scene).",
    "prealert_rejects": "Pre-alerts the simulated hospital explicitly rejected (advisor only).",
    "fallbacks": "Times the pre-alert moved to the next hospital after a reject or timeout (advisor only).",
    "nearest_prealert": "Reference policy, not a proposal: pre-alert 24x7 EDs nearest-first with the same simulated "
                        "answers but no advisor triage or ranking. Separates the gain from pre-alerting itself.",
    "delta_mean_ttt_critical": "advisor minus nearest mean time to treatment for critical patients; negative = "
                               "advisor better.",
}
SIMPLIFICATIONS = [
    "No fleet model: every incident has its own ambulance; dispatch and response are module A's job.",
    "Scene location is the H3 cell centre of the synthetic call.",
    "An accepted pre-alert is honoured on arrival, except for sim.accepted_prealert_refused_on_arrival_share "
    "(0 in the base case, swept).",
    "Hospitals answer a pre-alert instantly or not at all; an unanswered one costs prealert.response_timeout_s "
    "while the ambulance waits at the scene.",
    "If every pre-alerted hospital rejects, the crew drives unannounced to the advisor's top choice.",
    "Refusal chance depends only on the hospital's true ED status (sim.reject_*), the same with or without a "
    "pre-alert; after sim.max_refusals_per_patient refusals the next capable hospital must take the patient.",
    "ED offload queue is first-come first-served (no triage priority at the gate).",
    "STEMI counts as treated at a cath lab or a thrombolysis centre; onward PCI transfer is not modelled.",
    "Stroke counts as treated at CT + thrombolysis; thrombectomy is not scored separately.",
    "Gate dwell fed to the advisor's passive index = arrival to departure at the gate (offload wait + handover, or "
    "time to a refusal), as 108 GPS would see it.",
    "Travel uses free-flow shortest paths on the real OSM road graph with placeholder speeds and hourly congestion; "
    "actual drives add lognormal noise the advisor does not see.",
    "Hospital size class (ED bays, background load) is read from capabilities: PCI -> tertiary, CT -> secondary, "
    "else primary.",
]
CSV_CATS = ("stemi", "stroke", "severe_trauma", "obstetric", "paediatric", "arrest_airway", "low_acuity")


# ---------------------------------------------------------------- inputs


def load_sites(path: Path = SITES_BLR) -> list[Point]:
    with open(path, newline="") as f:
        return [(float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)]


def nearest_site(cells: dict[str, Point], sites: list[Point]) -> dict[str, Point]:
    """Base for each scene cell: the straight-line nearest GoldenHour standby site."""
    out = {}
    for h3, (lat, lon) in cells.items():
        p = LatLon(lat=lat, lon=lon)
        out[h3] = min(sites, key=lambda s: haversine_km(p, LatLon(lat=s[0], lon=s[1])))
    return out


@dataclass
class ScenarioSpec:
    """Everything a worker process needs to run one scenario (picklable)."""

    name: str
    raw: dict
    is_demo: bool
    hospitals: list[Hospital]
    tables: TravelTables
    incidents: list[Incident]
    bases: list[Point]
    draws: Draws
    origin: datetime
    categories: list[str]
    critical: list[str]


def run_job(job: tuple[ScenarioSpec, str]) -> tuple[dict, float]:
    """One policy on one scenario; returns (Metrics, seconds taken). Runs in a worker process."""
    spec, policy = job
    t0 = time.perf_counter()
    m = metrics(run_policy(_world(spec), policy), spec.categories, spec.critical)
    return m, time.perf_counter() - t0


def _world(spec: ScenarioSpec) -> World:
    """Build the scenario's World (config parsed from the scenario's raw dict)."""
    cfg = parse_config(spec.raw)
    hospitals = spec.hospitals
    if spec.is_demo:  # tier-share sweeps change the fictional capabilities, not the locations
        hospitals = build_demo_registry(cfg)
        if [(h.lat, h.lon) for h in hospitals] != [(h.lat, h.lon) for h in spec.hospitals]:
            raise ValueError("demo registry locations changed between scenarios; travel tables would not match")
    seed = int(cfg.v("sim.seed"))
    return World(incidents=spec.incidents, bases=spec.bases, hospitals=hospitals,
                 travel=SimTravel(spec.tables, cfg), cfg=cfg, draws=spec.draws, origin=spec.origin, seed=seed,
                 advisor_hospitals=registry_view(hospitals, float(cfg.v("sim.registry_error_share")), seed))


def _crit_mean(m: dict) -> float | None:
    return m["time_to_treatment"]["critical"]["mean"]


def sensitivity_row(sweep: Sweep | None, res: dict[str, dict], n: int) -> dict[str, Any]:
    near, adv = _crit_mean(res["nearest"]), _crit_mean(res["advisor"])
    delta = round(adv - near, 3) if near is not None and adv is not None else None
    return {
        "param": sweep.param if sweep else "(base)",
        "value": sweep.value if sweep else None,
        "label": sweep.label if sweep else "base config on the sweep window",
        "nearest_mean_ttt_critical": near, "advisor_mean_ttt_critical": adv,
        "delta_mean_ttt_critical": delta, "advisor_better": bool(delta is not None and delta < 0),
        "n_incidents": n,
        "nearest_secondary_transfer_rate": res["nearest"]["secondary_transfer_rate"],
        "advisor_secondary_transfer_rate": res["advisor"]["secondary_transfer_rate"],
        "nearest_prealert_mean_ttt_critical": _crit_mean(res["nearest_prealert"]),
        "nearest_ambulance_hours": res["nearest"]["ambulance_hours"],
        "advisor_ambulance_hours": res["advisor"]["ambulance_hours"],
    }


def inputs_provenance(is_demo: bool, registry_location_prov: dict[str, int]) -> dict[str, str]:
    hosp = Provenance.SIMULATED.value
    if not is_demo:
        order = [p.value for p in (Provenance.REAL, Provenance.REAL_DERIVED, Provenance.SIMULATED,
                                   Provenance.PLACEHOLDER)]
        hosp = max((k for k, v in registry_location_prov.items() if v), key=order.index, default=hosp)
    return {
        "incidents": "SIMULATED", "hospitals": hosp, "road_network": "REAL", "travel_speeds": "PLACEHOLDER",
        "travel_noise": "PLACEHOLDER", "ed_crowding": "SIMULATED", "ed_status_reports": "SIMULATED",
        "rejections": "SIMULATED", "patient_mix": "SIMULATED", "outcome_events": "SIMULATED",
        "gate_dwell": "SIMULATED", "advisor_scoring_params": "PLACEHOLDER",
    }


def _csv_rows(name: str, sweep: Sweep | None, res: dict[str, dict]) -> list[dict]:
    rows = []
    for policy in POLICIES:
        m = res[policy]
        t = m["time_to_treatment"]
        row = {
            "scenario": name, "param": sweep.param if sweep else "", "value": json.dumps(sweep.value) if sweep else "",
            "policy": policy, "n": m["n"],
            "mean_ttt_overall": t["overall"]["mean"], "p90_ttt_overall": t["overall"]["p90"],
            "mean_ttt_critical": t["critical"]["mean"], "p90_ttt_critical": t["critical"]["p90"],
        }
        for c in CSV_CATS:
            row[f"mean_ttt_{c}"] = t["by_category"].get(c, {}).get("mean")
        row.update({k: m[k] for k in ("secondary_transfers", "secondary_transfer_rate",
                                      "critical_capable_first_attempt_share", "ambulance_hours",
                                      "prealert_rejects", "fallbacks", "no_definitive_care")})
        row["provenance"] = "SIMULATED"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SimPy replay: nearest-hospital routing vs Destination Advisor")
    ap.add_argument("--quick", action="store_true", help=f"{QUICK_DAYS}-day base + 2 sweeps (writes to {QUICK_DIR})")
    ap.add_argument("--out", type=Path, default=None, help="output directory")
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)
    out_dir = args.out or (QUICK_DIR if args.quick else RESULTS_DIR)
    t_start = time.perf_counter()

    with open(args.config) as f:
        raw = yaml.safe_load(f)
    cfg = parse_config(raw)
    seed = int(cfg.v("sim.seed"))
    reg, is_demo = load_active_registry(cfg)
    hospitals = reg.hospitals

    calls = load_goldenhour_calls(cfg=cfg).incidents
    if not calls:
        raise SystemExit("no synthetic calls loaded")
    end = datetime.combine(calls[-1].when.date() + timedelta(days=1), datetime.min.time())
    days = QUICK_DAYS if args.quick else int(cfg.v("sim.replay_window_days"))
    start = end - timedelta(days=days)
    incidents = [c for c in calls if start <= c.when < end]
    origin = start - timedelta(hours=float(cfg.v("sim.warmup_hours")))
    print(f"[sim] {len(incidents)} synthetic incidents {start} .. {end}; {len(hospitals)} hospitals "
          f"({'FICTIONAL demo' if is_demo else 'verified'} registry)", flush=True)

    cells = load_cell_centres()
    sites = load_sites()
    base_of_cell = nearest_site(cells, sites)
    t0 = time.perf_counter()
    tables = load_or_compute_tables([(h.lat, h.lon) for h in hospitals], list(cells.values()), sites)
    print(f"[sim] travel tables ready in {time.perf_counter() - t0:.1f}s", flush=True)

    categories = list(cfg.care_categories)
    critical = critical_categories(cfg)  # fixed from the BASE config, so a lambda sweep cannot redefine it
    draws = Draws.make(len(incidents), len(hospitals), seed)
    bases = [base_of_cell[i.h3_index] for i in incidents]

    def spec(name: str, sraw: dict, idx: np.ndarray, s_origin: datetime) -> ScenarioSpec:
        return ScenarioSpec(name=name, raw=sraw, is_demo=is_demo, hospitals=hospitals, tables=tables,
                            incidents=[incidents[k] for k in idx], bases=[bases[k] for k in idx],
                            draws=draws.subset(idx), origin=s_origin, categories=categories, critical=critical)

    all_idx = np.arange(len(incidents))
    # sweep window: one contiguous seeded block of days inside the replay window (whole window if --quick)
    if args.quick:
        sw_idx, sw_start, sw_days = all_idx, start, days
    else:
        sw_days = min(int(cfg.v("sim.sweep_window_days")), days)
        first = int(np.random.default_rng([seed, 3]).integers(0, days - sw_days + 1))
        sw_start = start + timedelta(days=first)
        sw_end = sw_start + timedelta(days=sw_days)
        sw_idx = np.array([k for k, inc in enumerate(incidents) if sw_start <= inc.when < sw_end], dtype=int)
    sw_origin = sw_start - timedelta(hours=float(cfg.v("sim.warmup_hours")))
    sweeps = quick_sweeps(raw) if args.quick else default_sweeps(raw)

    specs = [spec("base", raw, all_idx, origin), spec("(base) sweep window", raw, sw_idx, sw_origin)]
    for sw in sweeps:
        sraw, _ = sw.config(raw)
        specs.append(spec(sw.label, sraw, sw_idx, sw_origin))
    print(f"[sim] running {len(specs)} scenarios x {len(POLICIES)} policies on {args.workers} worker(s); "
          f"sweep window {sw_start.date()} + {sw_days} days ({len(sw_idx)} incidents)", flush=True)

    # One job per scenario x policy; longest (the full-window base) first. Results are stored by index,
    # so the output order never depends on which worker finishes first.
    jobs = [(k, p) for k in range(len(specs)) for p in POLICIES]
    out: dict[tuple[int, str], dict] = {}
    t0 = time.perf_counter()

    def done(n_done: int, key: tuple[int, str], secs: float) -> None:
        print(f"[sim] {n_done}/{len(jobs)} done: {specs[key[0]].name} / {key[1]} "
              f"({len(specs[key[0]].incidents)} incidents, {secs:.1f}s; elapsed {time.perf_counter() - t0:.0f}s)",
              flush=True)

    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_job, (specs[k], p)): (k, p) for k, p in jobs}
            for n_done, fut in enumerate(as_completed(futs), start=1):
                out[futs[fut]], secs = fut.result()
                done(n_done, futs[fut], secs)
    else:
        for n_done, key in enumerate(jobs, start=1):
            out[key], secs = run_job((specs[key[0]], key[1]))
            done(n_done, key, secs)
    results = [{p: out[(k, p)] for p in POLICIES} for k in range(len(specs))]
    print(f"[sim] all scenarios done in {time.perf_counter() - t0:.1f}s", flush=True)

    base_res = results[0]
    sens = [sensitivity_row(None, results[1], len(sw_idx))]
    sens += [sensitivity_row(sw, r, len(sw_idx)) for sw, r in zip(sweeps, results[2:], strict=True)]
    sim_params = {k: {"value": p.value, "provenance": p.provenance.value, "basis": p.basis}
                  for k, p in sorted(cfg.params.items()) if k.startswith("sim.")}
    defs = dict(DEFINITIONS, critical=DEFINITIONS["critical"].format(critical=", ".join(critical)))
    doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "banner": DEMO_BANNER,
        "is_simulation": True,
        "mode": "quick" if args.quick else "full",
        "inputs_provenance": inputs_provenance(is_demo, reg.summary.location_provenance),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "n_incidents": len(incidents)},
        "sweep_window": {"start": sw_start.isoformat(), "end": (sw_start + timedelta(days=sw_days)).isoformat(),
                         "n_incidents": int(len(sw_idx))},
        "critical_categories": critical,
        "base": {"name": "base", "overrides": {}, "policies": base_res},
        "sensitivity": sens,
        "definitions": defs,
        "simplifications": SIMPLIFICATIONS,
        "sim_params": sim_params,
        "todo_params": cfg.todo_params(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "sim_results.json", "w") as f:
        json.dump(doc, f, sort_keys=True, indent=2, allow_nan=False)
        f.write("\n")
    rows = _csv_rows("base", None, base_res)
    rows += _csv_rows("(base) sweep window", None, results[1])
    for sw, r in zip(sweeps, results[2:], strict=True):
        rows += _csv_rows(sw.label, sw, r)
    with open(out_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"[sim] wrote {out_dir / 'sim_results.json'} and summary.csv in {time.perf_counter() - t_start:.1f}s")
    print("[sim] SIMULATION RESULTS ONLY: " + DEMO_BANNER)
    flips = [r["label"] for r in sens if not r["advisor_better"]]
    print(f"[sim] advisor better on critical mean TTT in {len(sens) - len(flips)}/{len(sens)} sweep scenarios"
          + (f"; not better in: {flips}" if flips else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
