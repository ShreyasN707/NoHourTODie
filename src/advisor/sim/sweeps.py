"""Sensitivity sweeps (spec §9): which assumptions could flip "the advisor beats nearest-hospital routing"?

Each sweep changes ONE value in a copy of the raw config dict (config/advisor.yaml) and re-parses it with
`parse_config`, so provenance checks still run. Values are chosen to bracket the base value widely, not
to favour either policy. Every scenario is replayed on the same seeded block of days with the same draws.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from advisor.config import AdvisorConfig, parse_config


@dataclass(frozen=True)
class Sweep:
    """One sensitivity scenario: `param` set to `value`. `apply` edits a raw config dict in place."""

    param: str
    value: Any
    label: str
    apply: Callable[[dict], None]

    def config(self, raw: dict) -> tuple[dict, AdvisorConfig]:
        new = copy.deepcopy(raw)
        self.apply(new)
        return new, parse_config(new)


def _set(key: str, value: Any) -> Callable[[dict], None]:
    section, name = key.split(".", 1)

    def apply(raw: dict) -> None:
        raw[section][name]["value"] = copy.deepcopy(value)
    return apply


def _set_item(key: str, item: str, value: Any) -> Callable[[dict], None]:
    section, name = key.split(".", 1)

    def apply(raw: dict) -> None:
        raw[section][name]["value"][item] = value
    return apply


def _scale(key: str, factor: float) -> Callable[[dict], None]:
    section, name = key.split(".", 1)

    def apply(raw: dict) -> None:
        raw[section][name]["value"] = [round(v * factor, 6) for v in raw[section][name]["value"]]
    return apply


def set_sweep(key: str, value: Any, label: str) -> Sweep:
    return Sweep(key, value, label, _set(key, value))


def default_sweeps(raw: dict) -> list[Sweep]:
    """The sensitivity grid. `raw` is used only to describe base values in labels."""
    base_reject = raw["sim"]["reject_base_rate"]["value"]
    sw: list[Sweep] = [
        set_sweep("scoring.ed_wait_by_status_min", {"green": 2, "amber": 8, "red": 15},
                  "advisor's ED-wait lookup about halved (2/8/15 min)"),
        set_sweep("scoring.ed_wait_by_status_min", {"green": 10, "amber": 30, "red": 60},
                  "advisor's ED-wait lookup doubled (10/30/60 min)"),
        set_sweep("scoring.transfer_delay_min", 20, "advisor's transfer delay 20 min"),
        set_sweep("scoring.transfer_delay_min", 90, "advisor's transfer delay 90 min"),
    ]
    for r in (0.02, 0.15, 0.30):
        sw.append(set_sweep("sim.reject_base_rate", r, f"per-trip refusal rate at a green ED {r} (base {base_reject})"))
    sw += [
        set_sweep("sim.stemi_share_without_ecg", 0.10, "10% of no-ECG chest pain is truly STEMI"),
        set_sweep("sim.stemi_share_without_ecg", 0.50, "50% of no-ECG chest pain is truly STEMI"),
        set_sweep("sim.chest_pain_ecg_shares", {"stemi": 0.30, "negative": 0.50, "none": 0.20},
                  "most chest pain gets an ECG (20% without)"),
        set_sweep("sim.trauma_severe_share", 0.10, "10% of trauma is severe"),
        set_sweep("sim.trauma_severe_share", 0.40, "40% of trauma is severe"),
        Sweep("scoring.lambda_by_category.low_acuity", 0.0, "lambda for low acuity 0 (no load balancing)",
              _set_item("scoring.lambda_by_category", "low_acuity", 0.0)),
        Sweep("scoring.lambda_by_category.low_acuity", 1.0, "lambda for low acuity 1.0",
              _set_item("scoring.lambda_by_category", "low_acuity", 1.0)),
        Sweep("travel.hourly_congestion", 0.75, "traffic multipliers x0.75 (lighter traffic)",
              _scale("travel.hourly_congestion", 0.75)),
        Sweep("travel.hourly_congestion", 1.25, "traffic multipliers x1.25 (heavier traffic)",
              _scale("travel.hourly_congestion", 1.25)),
        set_sweep("sim.travel_noise_sigma", 0.10, "travel-time noise sigma 0.10"),
        set_sweep("sim.travel_noise_sigma", 0.50, "travel-time noise sigma 0.50 (very unpredictable)"),
        set_sweep("sim.status_stale_share", 0.0, "no hospital's status report goes stale"),
        set_sweep("sim.status_stale_share", 0.50, "half of hospitals' status reports go stale"),
        set_sweep("sim.status_misreport_share", 0.0, "no hospital misreports its status"),
        set_sweep("sim.status_misreport_share", 0.30, "30% of hospitals always report green"),
        set_sweep("sim.status_misreport_share", 0.60, "60% of hospitals always report green"),
        set_sweep("sim.demo_tier_shares", {"tertiary": 0.1, "secondary": 0.4, "primary": 0.5},
                  "fewer tertiary hospitals (10% / 40% / 50%)"),
        set_sweep("sim.demo_tier_shares", {"tertiary": 0.3, "secondary": 0.4, "primary": 0.3},
                  "more tertiary hospitals (30% / 40% / 30%)"),
        set_sweep("sim.accepted_prealert_refused_on_arrival_share", 0.10,
                  "10% of accepted pre-alerts refused on arrival anyway"),
        set_sweep("sim.accepted_prealert_refused_on_arrival_share", 0.25,
                  "25% of accepted pre-alerts refused on arrival anyway"),
        set_sweep("sim.background_load_scale", 0.7, "EDs 30% less busy (background arrivals x0.7)"),
        set_sweep("sim.background_load_scale", 1.3, "EDs 30% busier (background arrivals x1.3)"),
        set_sweep("sim.prealert_no_answer_share", 0.30, "30% of pre-alerts go unanswered"),
        set_sweep("sim.registry_error_share", 0.05, "5% of the advisor's capability records are wrong"),
        set_sweep("sim.registry_error_share", 0.15, "15% of the advisor's capability records are wrong"),
        set_sweep("sim.registry_error_share", 0.30, "30% of the advisor's capability records are wrong"),
    ]
    return sw


def quick_sweeps(raw: dict) -> list[Sweep]:
    """Two scenarios for `--quick` dev runs."""
    return [set_sweep("scoring.transfer_delay_min", 20, "advisor's transfer delay 20 min"),
            set_sweep("sim.reject_base_rate", 0.30, "per-trip refusal rate at a green ED 0.3")]
