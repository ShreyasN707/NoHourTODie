"""FICTIONAL demo registry, used only while registry/hospitals.csv (the verified registry) is empty.

Why it exists: the demo screen, API and SimPy replay need hospitals with capabilities, and no public
capability dataset exists for Bengaluru (spec §7). Putting invented capabilities on real, named
hospitals would be a false claim about them, so we use fictional hospitals:
  - names "Demo Hospital NN (fictional)", ids "demo-NN"
  - locations: H3 cell centres from GoldenHour's Bengaluru grid, sampled weighted by WorldPop
    population, so hospitals sit where people live (as real ones do), without copying a real site
  - capabilities: tier bundles from config (sim.demo_tier_*), all SIMULATED, "TODO: source"
Every row is tagged SIMULATED with source DEMO_SOURCE. The UI and API must show a banner when used.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from advisor.config import AdvisorConfig, default_config
from advisor.models import Capability, Hospital
from advisor.provenance import Provenance, Tagged
from advisor.registry.loader import RegistryLoad, load_registry, summarize
from advisor.sim.incidents import CELLS_BLR

DEMO_SOURCE = "FICTIONAL demo registry (seeded from config sim.*); capability mix illustrative, TODO: source"
DEMO_TAG = Tagged(provenance=Provenance.SIMULATED, source=DEMO_SOURCE)


def build_demo_registry(cfg: AdvisorConfig | None = None, cells_csv: Path | str = CELLS_BLR) -> list[Hospital]:
    """Deterministic for a given config (seed, count, tier shares, tier bundles) and cell grid."""
    cfg = cfg or default_config()
    rng = np.random.default_rng(cfg.v("sim.seed"))
    n = int(cfg.v("sim.demo_hospital_count"))
    shares: dict[str, float] = cfg.v("sim.demo_tier_shares")
    bundles: dict[str, list[str]] = cfg.v("sim.demo_tier_capabilities")
    if set(shares) != set(bundles) or abs(sum(shares.values()) - 1) > 1e-9:
        raise ValueError("sim.demo_tier_shares must sum to 1 and name the same tiers as sim.demo_tier_capabilities")

    with open(cells_csv, newline="") as f:
        cells = [(float(r["lat"]), float(r["lon"]), float(r["pop"])) for r in csv.DictReader(f)]
    pop = np.array([c[2] for c in cells])
    picks = rng.choice(len(cells), size=n, replace=False, p=pop / pop.sum())

    tiers: list[str] = []
    for tier, share in shares.items():
        tiers += [tier] * round(share * n)
    tiers = (tiers + ["primary"] * n)[:n]
    rng.shuffle(tiers)

    out = []
    for i, (cell, tier) in enumerate(zip(picks, tiers, strict=True), start=1):
        lat, lon, _ = cells[cell]
        caps = {c: Capability(available=True, hours="24x7", **DEMO_TAG.model_dump()) for c in bundles[tier]}
        out.append(Hospital(
            hospital_id=f"demo-{i:02d}", name=f"Demo Hospital {i:02d} (fictional, {tier})", lat=lat, lon=lon,
            ownership=str(rng.choice(["govt", "private"])), location_tag=DEMO_TAG, capabilities=caps,
        ))
    return out


def load_active_registry(cfg: AdvisorConfig | None = None) -> tuple[RegistryLoad, bool]:
    """The verified registry if it has rows, else the fictional demo registry.

    Returns (registry, is_demo). Callers must surface is_demo (banner in UI, flag in API).
    """
    verified = load_registry()
    if verified.hospitals:
        return verified, False
    demo = build_demo_registry(cfg)
    return RegistryLoad(hospitals=demo, summary=summarize(demo, "demo (fictional)", "demo (fictional)")), True
