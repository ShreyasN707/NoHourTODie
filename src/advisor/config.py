"""Load config/advisor.yaml. All thresholds and placeholders live there, never inline (spec §14).

Each param must carry `provenance` and `basis`; loading fails loudly otherwise (spec §2 rule 3).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from advisor.provenance import Provenance, ProvenanceError

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT_DIR / "config" / "advisor.yaml"

PARAM_SECTIONS = ("scoring", "reliability", "travel", "passive", "rules", "sim", "prealert")


class Param(BaseModel):
    value: Any
    provenance: Provenance
    basis: str

    @property
    def is_set(self) -> bool:
        return self.value is not None

    @property
    def needs_source(self) -> bool:
        return "TODO: source" in self.basis


class CareCategory(BaseModel):
    name: str
    rule: str  # score | nearest | trauma | stemi | stroke
    required: list[str]
    preferred: list[str] = []
    primary_pci: list[str] = []
    pharmacoinvasive: list[str] = []
    lvo_target: list[str] = []


class AdvisorConfig(BaseModel):
    params: dict[str, Param]  # keyed "section.name"
    care_categories: dict[str, CareCategory]

    def p(self, key: str) -> Param:
        try:
            return self.params[key]
        except KeyError:
            raise KeyError(f"config param '{key}' not found in advisor.yaml") from None

    def v(self, key: str) -> Any:
        return self.p(key).value

    def lambda_for(self, category: str) -> float:
        return float(self.v("scoring.lambda_by_category")[category])

    def todo_params(self) -> list[str]:
        """Params with no source yet; listed in the README and in API provenance output."""
        return sorted(k for k, p in self.params.items() if p.needs_source)


def parse_config(raw: dict[str, Any]) -> AdvisorConfig:
    params: dict[str, Param] = {}
    for section in PARAM_SECTIONS:
        for name, body in (raw.get(section) or {}).items():
            key = f"{section}.{name}"
            if not isinstance(body, dict) or "provenance" not in body or not body.get("basis"):
                raise ProvenanceError(f"config param '{key}' must have value, provenance and basis")
            params[key] = Param(**body)
    cats = {
        name: CareCategory(name=name, **body) for name, body in (raw.get("care_categories") or {}).items()
    }
    cfg = AdvisorConfig(params=params, care_categories=cats)
    missing = set(cfg.v("scoring.lambda_by_category")) ^ set(cats)
    if missing:
        raise ValueError(f"lambda_by_category and care_categories disagree on: {sorted(missing)}")
    return cfg


def load_config(path: Path | str = DEFAULT_CONFIG) -> AdvisorConfig:
    with open(path) as f:
        return parse_config(yaml.safe_load(f))


@lru_cache(maxsize=1)
def default_config() -> AdvisorConfig:
    return load_config()
