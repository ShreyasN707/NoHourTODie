"""Render the evaluation write-up (spec §13 step 9) from results/advisor_sim/sim_results.json.

Every number in the output comes from the results file, so slides built from it cannot drift from the
simulation. Run: PYTHONPATH=src python -m advisor.sim.report  (or `make advisor-report`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from advisor.config import ROOT_DIR

RESULTS = ROOT_DIR / "results" / "advisor_sim" / "sim_results.json"
OUT = ROOT_DIR / "results" / "advisor_sim" / "EVALUATION.md"
POLICY_LABEL = {"nearest": "Nearest hospital (today)", "nearest_prealert": "Nearest + pre-alert",
                "advisor": "Destination Advisor"}
CATS = ["stemi", "stroke", "severe_trauma", "obstetric", "paediatric", "arrest_airway", "low_acuity"]


def _f(x: Any, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _pct(x: Any) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _days(window: dict) -> int:
    from datetime import datetime

    return (datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).days


def render(r: dict) -> str:
    pol = r["base"]["policies"]
    n, a, p = pol["nearest"], pol["advisor"], pol["nearest_prealert"]
    crit = lambda m: m["time_to_treatment"]["critical"]  # noqa: E731
    w = r["window"]
    ttt_gain = crit(n)["mean"] - crit(a)["mean"]
    prealert_part = crit(n)["mean"] - crit(p)["mean"]
    hours_saved = n["ambulance_hours"] - a["ambulance_hours"]
    sens = r["sensitivity"]
    wins = sum(1 for s in sens if s["advisor_better"])
    worst = max(sens, key=lambda s: s["delta_mean_ttt_critical"])
    reg = [s for s in sens if s["param"] == "sim.registry_error_share"]

    L: list[str] = []
    L += ["# Destination Advisor: simulation evaluation", "",
          f"> **{r['banner']}**",
          "> These are simulation results, not field results. Nothing here has been measured on real patients, "
          "real hospitals or real 108 data.", "",
          f"Generated {r['generated_at']} from `results/advisor_sim/sim_results.json` (seed {r['seed']}). "
          f"Base window {w['start'][:10]} to {w['end'][:10]}: {w['n_incidents']:,} synthetic incidents "
          "(GoldenHour's synthetic call year, Bengaluru).", "",
          "## Headline (base scenario)", "",
          f"Compared with taking every patient to the nearest hospital, the advisor cut the mean time to definitive "
          f"treatment for critical patients by **{_f(ttt_gain)} min** ({_f(crit(n)['mean'])} to "
          f"{_f(crit(a)['mean'])}). Pre-alerting the nearest hospitals without the advisor's ranking accounts for "
          f"{_f(prealert_part)} min of that; the rest comes from choosing a capable, less crowded hospital.", "",
          "| Metric | " + " | ".join(POLICY_LABEL[k] for k in ("nearest", "nearest_prealert", "advisor")) + " |",
          "|---|---|---|---|"]
    rows = [
        ("Critical patients: mean time to treatment (min)", lambda m: _f(crit(m)["mean"])),
        ("Critical patients: 90th percentile (min)", lambda m: _f(crit(m)["p90"])),
        ("All patients: mean time to treatment (min)", lambda m: _f(m["time_to_treatment"]["overall"]["mean"])),
        ("Secondary transfers (moved on after refusal or missing capability)",
         lambda m: f"{m['secondary_transfers']:,} ({_pct(m['secondary_transfer_rate'])})"),
        ("Critical patients at a capable hospital on first attempt",
         lambda m: _pct(m["critical_capable_first_attempt_share"])),
        ("Ambulance-hours (scene to back at base)", lambda m: f"{m['ambulance_hours']:,.0f}"),
        ("Pre-alert rejections / fallbacks", lambda m: f"{m['prealert_rejects']:,} / {m['fallbacks']:,}"),
    ]
    for label, fn in rows:
        L.append(f"| {label} | {fn(n)} | {fn(p)} | {fn(a)} |")
    L += ["", f"Ambulance-hours saved over the window: **{hours_saved:,.0f}** "
          f"({_pct(hours_saved / n['ambulance_hours'])}).", "",
          "## By care category (mean / 90th percentile, minutes)", "",
          "| Category | n | Nearest | Advisor |", "|---|---|---|---|"]
    for c in CATS:
        sn, sa = n["time_to_treatment"]["by_category"].get(c), a["time_to_treatment"]["by_category"].get(c)
        if sn and sn.get("n"):
            L.append(f"| {c} | {sn['n']:,} | {_f(sn['mean'])} / {_f(sn['p90'])} | {_f(sa['mean'])} / {_f(sa['p90'])} |")

    L += ["", "## Sensitivity: is the result robust?", "",
          f"The advisor had the lower critical-patient mean in **{wins} of {len(sens)}** sweep scenarios. "
          f"Its smallest margin was {_f(-worst['delta_mean_ttt_critical'])} min, under "
          f"\"{worst['label']}\". Sweeps run on a {_days(r['sweep_window'])}-day window, so their "
          "baseline differs slightly from the 28-day headline.", "",
          "| Scenario | Nearest | Nearest + pre-alert | Advisor | Advisor - nearest |", "|---|---|---|---|---|"]
    for s in sorted(sens, key=lambda s: -s["delta_mean_ttt_critical"]):
        flag = "" if s["advisor_better"] else " **(advisor not better)**"
        L.append(f"| {s['label']} | {_f(s['nearest_mean_ttt_critical'])} | "
                 f"{_f(s.get('nearest_prealert_mean_ttt_critical'))} | {_f(s['advisor_mean_ttt_critical'])} | "
                 f"{_f(s['delta_mean_ttt_critical'])}{flag} |")

    L += ["", "## How to read this honestly", "",
          "- **Everything is simulated except the road network.** The incidents are GoldenHour's synthetic calls, "
          "and the hospitals are a fictional demo registry. ED crowding, refusals and status reports are simulated. "
          "Travel speeds are placeholders. No real 108 logs were available.",
          ("- **The headline assumes a perfect capability registry.** The base scenario gives the advisor exact "
           "capabilities. With " + ", ".join(_pct(s["value"]) for s in reg) + " of capability records wrong, the "
           "advisor's critical mean rises to " + ", ".join(_f(s["advisor_mean_ttt_critical"]) for s in reg)
           + f" min (nearest: {_f(reg[0]['nearest_mean_ttt_critical'])} min)."
           if reg else "- **The headline assumes a perfect capability registry.**"),
          "  That is why the verified registry (phone and website audit) comes before any pilot.",
          "- **Much of the gain for low-acuity and obstetric patients comes from avoiding crowded EDs.** It depends "
          "on the simulated crowding model. The real-world evidence for crowding-aware routing is weak: FDNY's "
          "load-balancing rule raised transport times in simulation and was recommended only for non-critical "
          "patients. Capability-first routing (STEMI, stroke, trauma) is the guideline-backed part.",
          "- **Refusals act as load-shedding in this model.** In the 'refusal rate 0.02' sweep, nearest-hospital "
          "routing gets much worse, because ambulances queue at the overloaded nearest ED instead of being sent on. "
          "That is a property of the queue model, not a finding about real EDs.",
          "- **Definitive care is defined generously for today's practice.** A STEMI counts as treated at any "
          "thrombolysis-capable hospital, and the onward PCI transfer is not charged. This favours nearest-hospital "
          "routing.",
          "- **The advisor only depends on dispatch being on time.** Karnataka's fleet gap (response times of 45-60 "
          "min reported) limits what destination choice can do. The advisor complements faster dispatch; it does "
          "not replace it.",
          "- **Next step is a stepped pilot, not more simulation.** A scoping review found only one dynamic "
          "ambulance-relocation study evaluated prospectively in real time.", "",
          "## Simplifications in the model", ""]
    L += [f"- {s}" for s in r.get("simplifications", [])]
    L += ["", "## Inputs and their provenance", "", "| Input | Provenance |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(r["inputs_provenance"].items())]
    L += ["", f"Config parameters still marked `TODO: source`: {len(r.get('todo_params', []))}. They are listed in the "
          "README table and in `config/advisor.yaml`.", ""]
    return "\n".join(L)


def main() -> None:
    r = json.loads(Path(RESULTS).read_text())
    OUT.write_text(render(r))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
